"""分包增量编排；复用已有差分引擎，不推测尚未开放的官方补丁格式。"""

import hashlib
import json
import logging
import re
from typing import Any, Dict, Optional

from .core import ConfigError, NetworkError, WGameManager, WWError
from .incremental import IncrementalError, IncrementalManager, _is_diff_resource, _resource_rel_path

logger = logging.getLogger("WW_Manager")
PLAN = ".incremental_download/packs/plan.json"
HISTORY = ".ww_manager_cache/packs/plan.json"


def _digest(data: Dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class PackIncrementalManager(IncrementalManager):
    """给现有引擎绑定独立包的版本、配置和缓存，禁止写主版本。"""

    def __init__(self, owner: WGameManager, name: str, entry: Dict, plan: Dict):
        self.owner = owner
        self.pack_name = name
        self.entry = entry
        self.plan = plan
        owner._managed_path(f".ww_manager_cache/packs/{name}")
        owner._managed_path(f".incremental_download/packs/{name}")
        # 原引擎直接写入这两份元数据，先拒绝预置软链接，避免覆盖目录外文件。
        for metadata in ("indexFile.json", "version_info.json"):
            owner._managed_path(f".incremental_download/packs/{name}/{metadata}")
        owner._managed_path(f".ww_manager_cache/packs/{name}/indexFile.json")
        super().__init__(
            owner.game_folder, owner.server_type, {"default": {"config": entry["config"]}}, plan["cdn"], name
        )
        self._index_file = entry.get("index")

    def get_current_version(self) -> Optional[str]:
        return self.entry["from_version"]

    def validate_index(self, index: Any) -> None:
        """先整单验证，安装目标必须归属本包；传输用 krpdiff 仅存隔离缓存。"""
        if not isinstance(index, dict) or not any(k in index for k in ("resource", "groupInfos", "deleteFiles")):
            raise ConfigError(f"{self.pack_name} 补丁索引格式尚不支持，未修改游戏文件")
        outputs = set()

        def path(dest, installed=True):
            if not isinstance(dest, str) or dest.startswith(("/", "\\")):
                raise ConfigError(f"非法补丁路径: {dest!r}")
            rel = _resource_rel_path(dest)
            if not rel.parts or any(":" in p or "\x00" in p for p in rel.parts):
                raise ConfigError(f"非法补丁路径: {dest!r}")
            if installed:
                prefix = tuple(p.lower() for p in rel.parts[:3])
                if self.pack_name == "common":
                    if prefix[:2] == ("client", "content") and prefix[2:] in (("hd",), ("sd",), ("uhd",)):
                        raise ConfigError(f"common 清单越界到档位目录: {dest}")
                    if rel.parts[0].startswith((".", "wwm_", "launcherDownloadConfig")):
                        raise ConfigError(f"common 清单试图覆盖管理记录: {dest}")
                elif len(rel.parts) < 4 or rel.parts[:3] != ("Client", "Content", self.pack_name.upper()):
                    raise ConfigError(f"{self.pack_name} 清单越界: {dest}")
                self.owner._managed_path(rel.as_posix())
                self.owner._managed_path(rel.as_posix() + ".bak")
            # 下载、迁移、修复三个阶段的临时路径也不得经过软链接。
            for base in (self.download_dir, self.cache_dir):
                for directory in ("", "resources", "krpdiff_temp", "full_download_temp", "repair_download_temp"):
                    candidate = base / directory / rel
                    relative = candidate.relative_to(self.game_folder).as_posix()
                    self.owner._managed_path(relative)
                    self.owner._managed_path(relative + ".temp")
            if installed:
                target = self.game_folder / rel
                if target.exists() and not target.is_file():
                    raise ConfigError(f"补丁目标不是普通文件: {dest}")
            return rel.as_posix().lower()

        def file(item, installed=True, output=False):
            if not isinstance(item, dict):
                raise ConfigError("补丁文件记录不是对象")
            dest = path(item.get("dest"), installed)
            size, md5 = item.get("size"), item.get("md5")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ConfigError(f"补丁文件大小非法: {item}")
            if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
                raise ConfigError(f"补丁文件 MD5 非法: {item}")
            if output:
                if dest in outputs:
                    raise ConfigError(f"补丁输出路径重复: {item['dest']}")
                outputs.add(dest)

        for key in ("resource", "groupInfos", "deleteFiles"):
            if not isinstance(index.get(key, []), list):
                raise ConfigError(f"补丁索引 {key} 必须为数组")
        for item in index.get("resource", []):
            diff = isinstance(item, dict) and isinstance(item.get("dest"), str) and _is_diff_resource(item["dest"])
            file(item, installed=not diff, output=not diff)
        transfers = {item["dest"] for item in index.get("resource", []) if _is_diff_resource(item["dest"])}
        for group in index.get("groupInfos", []):
            if not isinstance(group, dict):
                raise ConfigError("补丁组必须为对象")
            path(group.get("dest"), installed=False)
            for key in ("srcFiles", "dstFiles"):
                if not isinstance(group.get(key), list) or not group[key]:
                    raise ConfigError(f"补丁组 {key} 为空或格式尚不支持")
                for item in group[key]:
                    # 完整资源可覆盖同一 dst 的差分输出，原引擎已定义此优先级。
                    file(item)
            if group["dest"] not in transfers:
                raise ConfigError(f"差分组缺少带大小和 MD5 的传输记录: {group['dest']}")
        group_outputs = {}
        for group in index.get("groupInfos", []):
            for item in group["dstFiles"]:
                dest = path(item["dest"])
                if dest in group_outputs:
                    raise ConfigError(f"差分组输出路径重复: {item['dest']}")
                group_outputs[dest] = item
        for dest in index.get("deleteFiles", []):
            if path(dest) in outputs or path(dest) in group_outputs:
                raise ConfigError(f"补丁同时写入并删除同一路径: {dest}")
        if self.pack_name == "common" and self.owner.server_type == "bilibili":
            channel_index = self.entry.get("channel_index")
            if not isinstance(channel_index, dict) or not isinstance(channel_index.get("resource"), list):
                raise ConfigError("尚无 B 服目标清单证明 common 兼容，拒绝借用官服基础文件")
            target = {_resource_rel_path(item["dest"]).as_posix().lower(): item for item in channel_index["resource"]}
            writes = [r for r in index.get("resource", []) if not _is_diff_resource(r["dest"])]
            writes += list(group_outputs.values())
            for item in writes:
                expected = target.get(path(item["dest"]))
                if not expected or any(item[k] != expected.get(k) for k in ("size", "md5")):
                    raise ConfigError(f"common 文件与 B 服目标清单不符: {item['dest']}")
            if any(path(dest) in target for dest in index.get("deleteFiles", [])):
                raise ConfigError("common 试图删除 B 服目标版本仍需要的文件")

    def get_group_infos(self):
        self.validate_index(self.index_file)
        return super().get_group_infos()

    def check_downloaded_files(self) -> None:
        """应用任一包之前，校验所有包的传输文件或已经就绪的正式文件。"""
        for item in self.entry["index"].get("resource", []):
            dest = item["dest"]
            size, md5 = item["size"], item["md5"]
            if _is_diff_resource(dest):
                candidates = [self.download_dir / _resource_rel_path(dest)]
            else:
                candidates = [
                    self.game_folder / _resource_rel_path(dest),
                    self._get_staged_complete_path(self.download_dir, dest),
                ]
            if not any(self._is_file_verified(p, size, md5) for p in candidates):
                raise ConfigError(f"{self.pack_name} 下载缓存缺失或损坏: {dest}；请重跑 ww incremental")

    def _update_local_version(self, version_or_data):
        version = self.entry["to_version"]
        if self.pack_name != "common":
            self.owner._register_installed_tier(self.pack_name, version, self.plan["bundle"])
        self.owner._record_resource_pack(self.pack_name, version)

    def _apply_delete_files(self, delete_files):
        existing = sum((self.game_folder / _resource_rel_path(dest)).exists() for dest in delete_files)
        removed = super()._apply_delete_files(delete_files)
        if removed != existing:
            raise IncrementalError(f"{self.pack_name} 废弃资源未能全部删除，未登记新版本")
        return removed


class ResourceUpdater:
    """固定 common + 所选档位的目标和索引；下载不动游戏，应用支持分包断点。"""

    def __init__(self, owner: WGameManager):
        self.owner = owner

    def has_pending_plan(self) -> bool:
        return self.owner._managed_path(PLAN).is_file()

    def _read_plan(self, relative=PLAN) -> Dict:
        path = self.owner._managed_path(relative)
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            tier = self.owner.current_resource_tier()
            if not isinstance(plan, dict) or plan.get("schema") != 1:
                raise ValueError("不支持的更新计划格式")
            if plan.get("server") != self.owner.server_type or plan.get("tier") != tier:
                raise ValueError("渠道或当前档位已经改变，请恢复下载时的渠道/档位，或重新下载更新计划")
            if not isinstance(plan.get("packs"), dict) or set(plan["packs"]) != {"common", tier}:
                raise ValueError("分包列表不匹配")
            if not isinstance(plan.get("bundle"), dict) or not isinstance(plan.get("cdn"), str):
                raise ValueError("缺少资源配置")
            for name, entry in plan["packs"].items():
                if not isinstance(entry, dict) or not isinstance(entry.get("config"), dict):
                    raise ValueError(f"{name} 配置损坏")
                if not all(isinstance(entry.get(k), str) and entry[k] for k in ("from_version", "to_version")):
                    raise ValueError(f"{name} 版本损坏")
                if entry["from_version"] != entry["to_version"]:
                    if not isinstance(entry.get("index"), dict) or _digest(entry["index"]) != entry.get("index_sha256"):
                        raise ValueError(f"{name} 索引与下载计划不一致")
            return plan
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise ConfigError(f"无法读取分包更新计划: {e}") from e

    def _create_plan(self, config_url: Optional[str]) -> Dict:
        bundle = self.owner.get_bundle_config(config_url)
        if not isinstance(bundle, dict) or not isinstance(bundle.get("resourcePacks"), dict):
            raise ConfigError("当前渠道未提供分包更新配置；不会改走其它档位或其它渠道")
        # 预下载字段格式须等正式开放后适配；不能把未知结构误当成当前版本。
        if bundle.get("predownload") is not None:
            raise ConfigError("分包预下载配置已开放，但格式尚未适配验证；请更新工具后重试")
        tier = self.owner.current_resource_tier()
        bundles = bundle.get("bundles", {})
        if not isinstance(bundles, dict) or not isinstance(bundles.get(tier.upper(), {}), dict):
            raise ConfigError("分包安装记录配置错误")
        nodes = [n for n in bundle.get("cdnList", []) if n.get("K1") == 1 and n.get("K2") == 1]
        if not nodes:
            raise ConfigError("分包配置中没有可用 CDN")
        plan = {
            "schema": 1,
            "server": self.owner.server_type,
            "tier": tier,
            "cdn": max(nodes, key=lambda n: n["P"])["url"],
            "bundle": bundle,
            "packs": {},
            "ready": False,
        }
        # 只消费官方明确提供的 resourcePacks；未来预下载配置从此入口适配，绝不拼猜版本路径。
        for name in ("common", tier):
            config = bundle["resourcePacks"].get(name)
            current = self.owner.resource_pack_version(name)
            if not current:
                raise ConfigError(f"无法确定 {name} 的源版本；请先用 ww resource {tier} 安装/修复所选档位")
            if not isinstance(config, dict) or not isinstance(config.get("version"), str) or not config["version"]:
                raise ConfigError(f"当前渠道的 {name} 更新配置尚未开放")
            target = config["version"]
            if all(re.fullmatch(r"\d+(?:\.\d+)*", v) for v in (current, target)):
                if tuple(map(int, target.split("."))) < tuple(map(int, current.split("."))):
                    raise ConfigError(f"{name} 线上目标 {target} 低于本地 {current}，拒绝降级")
            entry = {"from_version": current, "to_version": target, "config": config}
            plan["packs"][name] = entry
            if current == target:
                if name in self.owner._read_resource_state().get("incomplete", []):
                    raise ConfigError(f"{name} 上次更新未完成，不能按同版本直接跳过，请先修复")
                continue
            patches = config.get("patchConfig", [])
            if not isinstance(patches, list) or not any(
                isinstance(p, dict) and p.get("version") == current and p.get("indexFile") for p in patches
            ):
                raise ConfigError(
                    f"{name} 暂无 {current} → {target} 的匹配补丁（未开放或源版本不支持）；"
                    "请等待预下载渠道，或明确选择全量修复；不会自动下载 HD"
                )
            if name == "common" and self.owner.server_type == "bilibili":
                # 只共用画质包；common 的每一项变更还须符合 B 服目标全量清单。
                section = next(
                    (
                        s
                        for s in (self.owner.launcher_info.get(k) for k in ("predownload", "default"))
                        if isinstance(s, dict) and s.get("version") == target
                    ),
                    None,
                )
                uri = section.get("config", {}).get("indexFile") if section else None
                if not isinstance(uri, str):
                    raise ConfigError(f"B 服 {target} 目标清单尚未开放，不能确认 common 兼容性")
                from urllib.parse import urljoin

                channel_index = self.owner._http_get_json(urljoin(self.owner.cdn_node, uri))
                if not isinstance(channel_index, dict):
                    raise NetworkError("无法获取 B 服目标清单，未修改游戏")
                entry["channel_index"] = channel_index
            engine = PackIncrementalManager(self.owner, name, entry, plan)
            url = engine.get_index_url()
            index = self.owner._http_get_json(url) if url else None
            if not isinstance(index, dict):
                raise NetworkError(f"无法获取 {name} 补丁索引，未开始下载或修改游戏")
            engine.validate_index(index)
            entry["index"] = index
            entry["index_sha256"] = _digest(index)
        if all(entry["from_version"] == entry["to_version"] for entry in plan["packs"].values()):
            raise ConfigError("没有可用分包更新：预下载尚未开放或当前版本已是最新，未创建更新计划")
        return plan

    def download(self, config_url: Optional[str] = None) -> bool:
        try:
            # 已有计划按原目标续传，避免预下载维护前后线上配置改变导致缓存串用。
            plan = self._read_plan() if self.has_pending_plan() else self._create_plan(config_url)
            self._validate_sources(plan)
            self.owner._write_local_json(PLAN, plan)
            for name in ("common", plan["tier"]):
                entry = plan["packs"][name]
                if self.owner.resource_pack_version(name) == entry["to_version"]:
                    continue
                logger.info(f"下载 {name}: {entry['from_version']} → {entry['to_version']}")
                if not PackIncrementalManager(self.owner, name, entry, plan).download_incremental():
                    raise NetworkError(f"{name} 下载失败，游戏未更新；重跑 ww incremental 可续传")
            plan["ready"] = True
            self.owner._write_local_json(PLAN, plan)
            return True
        except (OSError, IncrementalError) as e:
            raise WWError(f"分包增量下载失败: {e}") from e

    def _validate_sources(self, plan: Dict) -> None:
        for name, entry in plan["packs"].items():
            current = self.owner.resource_pack_version(name)
            if current not in (entry["from_version"], entry["to_version"]):
                raise ConfigError(f"{name} 源版本已改变，拒绝应用旧计划: {current}")
            if entry["from_version"] != entry["to_version"]:
                PackIncrementalManager(self.owner, name, entry, plan).validate_index(entry["index"])

    def apply(self) -> bool:
        try:
            plan = self._read_plan()
            if not plan.get("ready"):
                raise ConfigError("分包资源尚未全部下载成功，请先重跑 ww incremental")
            self._validate_sources(plan)
            # 全部缓存预检通过，才能修改第一个包，避免 common 更新后才发现 UHD 缓存不符。
            for name, entry in plan["packs"].items():
                if self.owner.resource_pack_version(name) == entry["to_version"]:
                    continue
                engine = PackIncrementalManager(self.owner, name, entry, plan)
                directory = engine.download_dir
                info = json.loads((directory / "version_info.json").read_text(encoding="utf-8"))
                index = json.loads((directory / "indexFile.json").read_text(encoding="utf-8"))
                if (
                    info
                    != {
                        "server": plan["server"],
                        "from_version": entry["from_version"],
                        "to_version": entry["to_version"],
                    }
                    or _digest(index) != entry["index_sha256"]
                ):
                    raise ConfigError(f"{name} 缓存版本或索引与更新计划不匹配，未开始应用")
                engine.check_downloaded_files()
            for name in ("common", plan["tier"]):
                entry = plan["packs"][name]
                if self.owner.resource_pack_version(name) == entry["to_version"]:
                    continue
                self.owner._record_resource_pack(name, entry["from_version"], incomplete=True)
                if name != "common":
                    self.owner._unregister_installed_tier(name, self.owner._read_local_config())
                logger.info(f"应用 {name}: {entry['from_version']} → {entry['to_version']}")
                if not PackIncrementalManager(self.owner, name, entry, plan).apply_incremental():
                    raise WWError(f"{name} 应用未完成；主版本未升级，请勿启动游戏，重跑 ww incremental --apply 续作")
            cfg = self.owner._read_local_config()
            cfg.update(
                {
                    "version": plan["packs"]["common"]["to_version"],
                    "appId": self.owner.config["appId"],
                    "group": "default",
                }
            )
            self.owner._write_local_json("launcherDownloadConfig.json", cfg)
            self.owner._write_local_json(HISTORY, plan)
            self.owner._managed_path(PLAN).unlink()
            logger.info(f"common + {plan['tier']} 全部应用成功；未更新其它档位")
            return True
        except (OSError, ValueError, IncrementalError) as e:
            raise WWError(f"分包增量应用失败: {e}；请勿启动游戏，可重跑命令继续") from e

    def verify(self) -> bool:
        if self.has_pending_plan():
            raise ConfigError("分包更新尚未应用完成；请先运行 ww incremental --apply，再校验新版本")
        plan = self._read_plan(HISTORY)
        self._validate_sources(plan)
        if any(self.owner.resource_pack_version(name) != entry["to_version"] for name, entry in plan["packs"].items()):
            raise ConfigError("当前版本与上次更新记录不符，拒绝使用旧记录修复或回退版本")
        for name, entry in plan["packs"].items():
            if entry["from_version"] == entry["to_version"]:
                logger.info(f"{name} 此次无更新文件；不等同于全量校验")
                continue
            engine = PackIncrementalManager(self.owner, name, entry, plan)
            engine.validate_index(entry["index"])
            if not entry["index"].get("groupInfos") and not entry["index"].get("resource"):
                logger.info(f"{name} 此次没有可校验的新文件；不等同于全量校验")
                continue
            if not engine.verify_new_version():
                return False
        return True
