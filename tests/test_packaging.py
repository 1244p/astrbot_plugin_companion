"""打包产物的自检：zip 结构必须能被 AstrBot 的上传安装流程接受。

AstrBot 的 `install_plugin_from_file` 做三件事：
1. `validate_plugin_archive` 找 metadata.yaml（允许在唯一的顶层目录里）
2. 解压后把顶层目录剥掉，得到真正的插件目录
3. 按 metadata 的 `name` 决定安装目录名，要求是合法 Python 标识符

这里不 import astrbot（本机装不下它那一堆依赖），而是把这三条规则
按源码复刻一遍。源码位置：`astrbot/core/star/updater.py:325`、
`astrbot/core/zip_updater.py:267`、`astrbot/core/star/star_manager.py:627`。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_zip  # noqa: E402
from api import schema  # noqa: E402

REQUIRED_FIELDS = ("name", "desc", "version", "author")


def root_dir_of(entries: list[str]) -> str:
    """复刻 zip_updater._resolve_archive_root_dir 的 commonpath 逻辑。"""
    normalized = [os.path.normpath(e) for e in entries]
    portable = [e.replace("\\", "/") for e in normalized]
    candidates: list[str] = []
    for raw, norm, port in zip(entries, normalized, portable):
        if norm == ".":
            continue
        has_children = any(
            other != port and other.startswith(f"{port}/") for other in portable
        )
        if raw.endswith(("/", "\\")) or has_children:
            candidates.append(norm)
            continue
        parent, _, _ = port.rpartition("/")
        if not parent:
            return ""
        candidates.append(parent.replace("/", os.sep))
    if not candidates:
        return ""
    return os.path.commonpath(candidates)


class ArchiveShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.zip_path = os.path.join(cls.tmp.name, "plugin.zip")
        build_zip.build(cls.zip_path)
        with zipfile.ZipFile(cls.zip_path) as z:
            cls.entries = z.namelist()
            cls.metadata_text = z.read(f"{build_zip.PLUGIN_DIR_NAME}/metadata.yaml").decode("utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_single_root_dir_matching_plugin_name(self):
        self.assertEqual(root_dir_of(self.entries), build_zip.PLUGIN_DIR_NAME)

    def test_metadata_present_and_parsed(self):
        self.assertIn(f"{build_zip.PLUGIN_DIR_NAME}/metadata.yaml", self.entries)
        meta = {}
        for line in self.metadata_text.splitlines():
            if ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        for field in REQUIRED_FIELDS:
            self.assertIn(field, meta, f"metadata.yaml 缺 {field}")
            self.assertTrue(meta[field], f"metadata.yaml 的 {field} 为空")
        self.assertEqual(meta["name"], build_zip.PLUGIN_DIR_NAME)
        self.assertTrue(meta["name"].isidentifier(), "插件名必须是合法 Python 标识符")

    def test_runtime_files_included(self):
        for rel in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "core/service.py",
            "api/routes.py",
            "views/companion/index.html",
            "views/companion/app.js",
            "views/companion/style.css",
            "scripts/migrate_from_evo.py",
        ):
            self.assertIn(f"{build_zip.PLUGIN_DIR_NAME}/{rel}", self.entries)

    def test_dev_files_excluded(self):
        for entry in self.entries:
            name = entry.split("/", 1)[-1]
            self.assertFalse(
                name.startswith(("tests/", ".git/", ".preview/", "dist/")),
                f"开发文件不该进包: {entry}",
            )
            self.assertNotIn("__pycache__", entry)
            self.assertFalse(name.endswith((".pyc", ".db", ".log")), entry)
        self.assertNotIn(f"{build_zip.PLUGIN_DIR_NAME}/scripts/build_zip.py", self.entries)

    def test_conf_schema_matches_groups(self):
        """每个配置项都要落到一个真实分组，否则面板里它会静默消失。"""
        with open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8") as f:
            conf = json.load(f)
        group_ids = {gid for gid, _title, _desc in schema.GROUPS}
        for key in conf:
            if key == "baseline":
                continue
            self.assertIn(schema.group_of(key), group_ids, f"配置项 {key} 没有落到任何分组")
        self.assertTrue(group_ids, "schema.GROUPS 不能为空")


if __name__ == "__main__":
    unittest.main()
