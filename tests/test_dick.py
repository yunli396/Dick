import contextlib
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib import error as urlerror
from urllib import request as urlrequest

try:
    import compression.zstd as zstd
except ImportError:
    zstd = None

from dick import catalog, selfmanage
from dick.ai import Translator
from dick.cache import Cache
from dick.cli import choose_candidates, main
from dick.config import LAST_SOURCE, PRIORITIES, SOURCES, Settings
from dick.discovery import apk_repositories, apt_repositories, discover, dnf_repositories, pacman_repositories
from dick.index import Index
from dick.install import Installer
from dick.local import FILE_LISTS, LIST_COMMANDS, PARSERS, matches, rank, read_installed
from dick.models import DickError, LocalPackage, Package, Repository
from dick.network import HTTPClient, decompress
from dick.parsers import apk_packages, apt_packages, dnf_packages, pacman_packages, strip_nix_attribute
from dick.progress import Progress
from dick.security import (certificate_names, ensure_certificate, interface_addresses, resolve_token,
                           ssl_context, token_path)
from dick.syntax import Action, normalize, options
from dick.web import Handler, WebApp


def pacman_database(name="firefox", version="130.0-1", description="Web browser"):
    output = io.BytesIO()
    record = f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n%DESC%\n{description}\n\n%ARCH%\nx86_64\n".encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(f"{name}-{version}/desc")
        member.size = len(record)
        archive.addfile(member, io.BytesIO(record))
    return output.getvalue()


PRIMARY = b'''<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">
<package type="rpm"><name>firefox</name><arch>x86_64</arch>
<version epoch="1" ver="130.0" rel="2"/><summary>Web browser</summary></package>
<package type="rpm"><name>foreign</name><arch>aarch64</arch>
<version epoch="0" ver="1" rel="1"/><summary>Foreign architecture</summary></package>
</metadata>'''


def apk_index(*records):
    """造一个 APKINDEX.tar.gz；records 是 (name, version, description[, arch])。

    真实索引里 `P:`（包名）不是第一条字段，首条是校验和，这里照抄同样的形状。
    """
    entries = []
    for name, version, description, *rest in records:
        architecture = rest[0] if rest else "x86_64"
        entries.append(f"C:Q1MQxXAVMr80Ty95MYYNhPNBn/WDs=\nP:{name}\nV:{version}\n"
                       f"A:{architecture}\nS:866999\nI:1720320\nT:{description}\n"
                       f"U:https://example.com/\nL:MIT\n")
    payload = "\n".join(entries).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("APKINDEX")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        description = b"Alpine package index\n"
        meta = tarfile.TarInfo("DESCRIPTION")
        meta.size = len(description)
        archive.addfile(meta, io.BytesIO(description))
    return output.getvalue()


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("etc/os-release", 'ID=arch\nVERSION_ID="2026.10"\n')
        # 用夹具里的空配置：否则会读到开发者本机的 ~/.config/dick/config.toml，
        # enabled/priority 随本机配置变化，测试就不可复现了。
        self.config = self.write("etc/dick.toml", "")
        self.settings = Settings(config_path=self.config, root=self.root,
                                 cache_dir=self.root / "cache", create=True)
        self.settings.architecture = "x86_64"
        self.cache = Cache(self.settings.cache_dir)
        self.addCleanup(self.cache.close)
        self.messages = []

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def index(self, repositories, client=None):
        return Index(self.settings, self.cache, client or HTTPClient(), repositories, self.messages.append)


class DiscoveryTests(FixtureTest):
    def test_pacman_sections_includes_and_mirrors(self):
        self.write("etc/pacman.conf", "[options]\nArchitecture = auto\n[core]\nInclude = /etc/pacman.d/mirrorlist\n[extra]\nInclude = /etc/pacman.d/mirrorlist\n")
        self.write("etc/pacman.d/mirrorlist", "# Server = https://disabled/$repo\nServer = https://one/$repo/os/$arch\nServer = https://two/$repo/os/$arch\n")
        repositories = pacman_repositories(self.settings)
        self.assertEqual([repository.name for repository in repositories], ["core", "extra"])
        self.assertEqual(repositories[1].urls, ("https://one/extra/os/x86_64/extra.db",
                                                "https://two/extra/os/x86_64/extra.db"))

    def test_pacman_include_cycle_is_reported(self):
        self.write("etc/pacman.conf", "[core]\nInclude = /etc/pacman.conf\n")
        with self.assertRaisesRegex(DickError, "循环"):
            pacman_repositories(self.settings)

    def test_apt_list_deb822_and_flat_repository(self):
        self.write("etc/apt/sources.list", "deb [arch=amd64 signed-by=/key] https://mirror stable main extra\ndeb-src https://mirror stable main\ndeb [arch=arm64] https://foreign stable main\n")
        self.write("etc/apt/sources.list.d/vendor.sources", "Types: deb\nURIs: https://vendor\nSuites: stable\nComponents: main\nArchitectures: amd64\n\nTypes: deb\nURIs: https://disabled\nSuites: stable\nComponents: main\nEnabled: no\n")
        self.write("etc/apt/sources.list.d/flat.list", "deb https://flat ./\n")
        repositories = apt_repositories(self.settings)
        urls = [repository.urls[0] for repository in repositories]
        self.assertEqual(len(urls), 4)
        self.assertIn("https://mirror/dists/stable/extra/binary-amd64/Packages.xz", urls)
        self.assertIn("https://flat/Packages.xz", urls)
        self.assertFalse(any("disabled" in url or "foreign" in url for url in urls))

    def test_dnf_enabled_and_variable_expansion(self):
        self.write("etc/yum.repos.d/test.repo", "[main]\nbaseurl=https://repo/$releasever/$basearch/\n[disabled]\nenabled=0\nbaseurl=https://disabled\n[mirrored]\nmetalink=https://mirrors/?arch=${basearch}\n")
        repositories = dnf_repositories(self.settings)
        self.assertEqual(len(repositories), 2)
        self.assertEqual(repositories[0].urls, ("https://repo/2026/x86_64/",))
        self.assertEqual(repositories[1].metalink, "https://mirrors/?arch=x86_64")

    def test_flatpak_discovery_uses_only_allowed_command(self):
        with patch.object(self.settings, "available", side_effect=lambda source: source == "flatpak"), \
                patch("dick.discovery.native_output", return_value="flathub\thttps://dl.flathub.org/repo/\n") as output:
            repositories, errors = discover(self.settings)
        self.assertFalse(errors)
        self.assertIn("flatpak", [repository.source for repository in repositories])
        output.assert_called_once_with(["flatpak", "remotes", "--columns=name,url"])

    def test_linyaps_repositories_come_from_repo_show(self):
        payload = json.dumps({"defaultRepo": "stable", "version": 1, "repos": [
            {"name": "stable", "url": "https://repo.example.com/stable", "priority": 0},
            {"name": "nightly", "url": "https://repo.example.com/nightly", "priority": 1},
            {"name": ""},
        ]})
        with patch.object(self.settings, "available", side_effect=lambda source: source == "linyaps"), \
                patch("dick.discovery.native_output", return_value=payload) as output:
            repositories, errors = discover(self.settings)
        self.assertFalse(errors)
        linyaps = [repository for repository in repositories if repository.source == "linyaps"]
        self.assertEqual([repository.name for repository in linyaps], ["stable", "nightly"])
        self.assertEqual(linyaps[0].urls, ("https://repo.example.com/stable",))
        output.assert_called_once_with(["ll-cli", "--json", "repo", "show"])

    def test_linyaps_discovery_falls_back_to_single_repository(self):
        cases = [{"side_effect": DickError("ll-cli 不可用")}, {"return_value": "not json"},
                 {"return_value": json.dumps(["unexpected"])}, {"return_value": json.dumps({"repos": "nope"})}]
        for case in cases:
            with self.subTest(case=case), \
                    patch.object(self.settings, "available", side_effect=lambda source: source == "linyaps"), \
                    patch("dick.discovery.native_output", **case):
                repositories, errors = discover(self.settings)
            self.assertFalse(errors)
            self.assertEqual([(repository.source, repository.name) for repository in repositories
                              if repository.source == "linyaps"], [("linyaps", "linglong")])

    def test_disabled_sources_are_skipped_unless_explicitly_requested(self):
        config = self.write("conf.toml", '[sources]\nenabled = ["pacman"]\n')
        settings = Settings(config, self.root, self.root / "conf-cache")
        self.write("etc/pacman.conf", "[core]\nServer = https://one/$repo/os/$arch\n")
        with patch("dick.discovery.native_output") as output:
            repositories, errors = discover(settings)
            self.assertEqual([repository.source for repository in repositories], ["pacman"])
            output.assert_not_called()
            repositories, errors = discover(settings, respect_enabled=False, native=False)
        self.assertIn("aur", [repository.source for repository in repositories])
        self.assertFalse(errors)

    def test_native_probe_can_be_skipped(self):
        self.write("etc/pacman.conf", "[core]\nServer = https://one/$repo/os/$arch\n")
        with patch.object(self.settings, "available", return_value=True), \
                patch("dick.discovery.native_output", return_value="flathub\thttps://dl.flathub.org/repo/\n"):
            with_probe, _ = discover(self.settings)
            without_probe, errors = discover(self.settings, native=False)
        self.assertIn("flathub", [repository.name for repository in with_probe if repository.source == "flatpak"])
        self.assertNotIn("flatpak", [repository.source for repository in without_probe])

    def test_invalid_config_is_readable_error(self):
        config = self.write("bad.toml", '[syntax]\nprefer = []\n')
        with self.assertRaises(DickError):
            Settings(config, self.root, self.root / "other-cache")


    def test_apk_repositories_strip_tags_and_map_architectures(self):
        """/etc/apk/repositories 每行是一个仓库目录，@tag 前缀只影响包标签，索引照读。"""
        self.write("etc/apk/repositories",
                   "# 注释行\n"
                   "https://dl-cdn.alpinelinux.org/alpine/v3.20/main\n"
                   "@testing https://dl-cdn.alpinelinux.org/alpine/edge/testing\n"
                   "/media/cdrom/apks\n")
        repositories = apk_repositories(self.settings)
        self.assertEqual([repository.name for repository in repositories], ["v3.20/main", "edge/testing"])
        self.assertEqual(repositories[0].urls,
                         ("https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/APKINDEX.tar.gz",))
        self.assertEqual(repositories[0].source, "apk")
        self.settings.architecture = "armv7l"
        self.assertIn("/armv7/APKINDEX.tar.gz", apk_repositories(self.settings)[0].urls[0])

    def test_apk_without_configuration_and_guix_nixpkgs_pseudo_repositories(self):
        self.assertEqual(apk_repositories(self.settings), [])
        with patch.object(self.settings, "available", side_effect=lambda source: source in {"guix", "nixpkgs"}):
            repositories, errors = discover(self.settings)
        self.assertFalse(errors)
        on_demand = {repository.source: repository.urls for repository in repositories
                     if repository.source in {"guix", "nixpkgs"}}
        self.assertEqual(on_demand, {"guix": ("https://guix.gnu.org",),
                                     "nixpkgs": ("https://channels.nixos.org",)})


class ParserTests(unittest.TestCase):
    def test_pacman_tar_without_extraction(self):
        package = list(pacman_packages(pacman_database(), Repository("pacman", "extra", ())))[0]
        self.assertEqual((package.name, package.version, package.description), ("firefox", "130.0-1", "Web browser"))

    def test_apt_compression_and_multiline_description(self):
        raw = b"Package: firefox\nVersion: 130\nArchitecture: amd64\nDescription: Browser\n More detail\n\nPackage: other\nVersion: 1\nDescription: Other\n"
        for compressor in (gzip.compress, lzma.compress, bytes):
            with self.subTest(compressor=compressor):
                packages = list(apt_packages(compressor(raw), Repository("apt", "main", ())))
                self.assertEqual(len(packages), 2)
                self.assertEqual(packages[0].description, "Browser\nMore detail")

    def test_dnf_epoch_architecture_and_gzip(self):
        packages = list(dnf_packages(gzip.compress(PRIMARY), Repository("dnf", "main", (), "x86_64")))
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].version, "1:130.0-2")

    def test_invalid_index_is_rejected(self):
        with self.assertRaises(DickError):
            list(apt_packages(b"<html>Mirror error</html>", Repository("apt", "main", ())))
        with self.assertRaises(DickError):
            list(dnf_packages(b"<html/>", Repository("dnf", "main", ())))
        with self.assertRaises(DickError):
            list(pacman_packages(b"invalid tar", Repository("pacman", "extra", ())))

    def test_apk_index_members_fields_and_architectures(self):
        content = apk_index(("firefox", "128.0-r1", "Web browser"),
                            ("py3-requests", "2.33.1-r0", "HTTP request library", "aarch64"))
        packages = list(apk_packages(content, Repository("apk", "v3.20/main", ())))
        self.assertEqual([(package.name, package.version, package.description, package.architecture)
                          for package in packages],
                         [("firefox", "128.0-r1", "Web browser", "x86_64"),
                          ("py3-requests", "2.33.1-r0", "HTTP request library", "aarch64")])
        self.assertEqual((packages[0].source, packages[0].repository), ("apk", "v3.20/main"))

    def test_apk_index_errors_are_described(self):
        with self.assertRaisesRegex(DickError, "损坏的 APKINDEX.tar.gz"):
            list(apk_packages(b"invalid tar", Repository("apk", "main", ())))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            payload = b"just a description\n"
            member = tarfile.TarInfo("DESCRIPTION")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(DickError, "没有 APKINDEX"):
            list(apk_packages(output.getvalue(), Repository("apk", "main", ())))
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:  # 不压缩，方便这里省掉 P: 字段
            payload = b"C:Q1MQxXAVMr80Ty95MYYNhPNBn/WDs=\nV:1.0-r0\nT:No name here\n"
            member = tarfile.TarInfo("APKINDEX")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(DickError, "P 字段"):
            list(apk_packages(raw.getvalue(), Repository("apk", "main", ())))

    def test_nix_attribute_paths_become_installable_names(self):
        self.assertEqual(strip_nix_attribute("legacyPackages.x86_64-linux.python3Packages.requests"),
                         "python3Packages.requests")
        self.assertEqual(strip_nix_attribute("packages.aarch64-darwin.firefox"), "firefox")
        self.assertEqual(strip_nix_attribute("firefox"), "firefox")

    def test_expansion_and_zstd_limits(self):
        if zstd is None:
            self.skipTest("当前 Python 没有 compression.zstd")
        with self.assertRaisesRegex(DickError, "大小限制"):
            decompress(gzip.compress(b"a" * 1000), limit=20)
        compressed = zstd.compress(b"zstd works")
        self.assertEqual(decompress(compressed), b"zstd works")
        with self.assertRaisesRegex(DickError, "损坏"):
            decompress(b"\x28\xb5\x2f\xfd")


class CacheTests(FixtureTest):
    def test_atomic_rollback_preserves_last_snapshot(self):
        repository = Repository("pacman", "extra", ())
        original = Package("firefox", "pacman", "old", "1", "extra")
        self.cache.replace(repository, [original])
        snapshot = self.cache.snapshot(repository)

        def broken_packages():
            yield Package("firefox", "pacman", "new", "2", "extra")
            raise DickError("truncated index")

        with self.assertRaises(DickError):
            self.cache.replace(repository, broken_packages())
        self.assertEqual(self.cache.search("firefox", ["pacman"]), [original])
        self.assertEqual(self.cache.snapshot(repository), snapshot)

    def test_search_wildcards_are_literal(self):
        repository = Repository("apt", "main", ())
        self.cache.replace(repository, [Package("a_%", "apt", repository="main"),
                                        Package("abc", "apt", repository="main")])
        self.assertEqual([package.name for package in self.cache.search("_%", ["apt"])], ["a_%"])

    def test_query_cache_ttl_and_invalidation(self):
        packages = [Package("firefox", "aur", "Browser", "130", "aur")]
        with patch("dick.cache.time.time", return_value=100):
            self.cache.query_put("aur", "search:firefox", packages)
        with patch("dick.cache.time.time", return_value=110):
            self.assertEqual(self.cache.query_get("aur", "search:firefox", 20), packages)
        with patch("dick.cache.time.time", return_value=130):
            self.assertIsNone(self.cache.query_get("aur", "search:firefox", 20))
        self.cache.invalidate_queries(["aur"])
        self.assertIsNone(self.cache.query_get("aur", "search:firefox", 10000))


class IndexTests(FixtureTest):
    def test_refresh_runs_repositories_in_parallel(self):
        repositories = [Repository("pacman", name, ()) for name in ("one", "two", "three")]
        barrier = threading.Barrier(3)
        thread_names = set()

        def refresh(repository):
            thread_names.add(threading.current_thread().name)
            barrier.wait(timeout=2)
            return self.cache.replace(repository, [Package(repository.name, "pacman", repository=repository.name)])

        index = self.index(repositories)
        with patch.object(index, "refresh_repository", side_effect=refresh) as refresh_mock:
            index.refresh(["pacman"], workers=3)
        self.assertEqual(refresh_mock.call_count, 3)
        self.assertGreaterEqual(len(thread_names), 2)

    def test_install_candidates_preserve_configured_repository_priority(self):
        repositories = [Repository("pacman", "optimized", ()), Repository("pacman", "core", ())]
        for repository in repositories:
            self.cache.replace(repository, [Package("firefox", "pacman", repository=repository.name)])
        packages, errors = self.index(repositories).search("firefox", ["pacman"], exact=True)
        self.assertFalse(errors)
        self.assertEqual([package.repository for package in packages], ["optimized", "core"])

    def test_real_local_mirror_failover_and_offline_search(self):
        broken = self.write("mirror/broken.db", b"corrupt tar")
        good = self.write("mirror/extra.db", pacman_database())
        repository = Repository("pacman", "extra", (broken.as_uri(), good.as_uri()), "x86_64")
        index = self.index([repository])
        packages, errors = index.search("browser", ["pacman"])
        self.assertFalse(errors)
        self.assertEqual(packages[0].name, "firefox")
        with patch.object(index.client, "get", side_effect=AssertionError("Search must use the cache")):
            self.assertEqual(index.search("firefox", ["pacman"], exact=True)[0], packages)

    def test_failed_refresh_keeps_old_packages(self):
        mirror = self.write("mirror/extra.db", pacman_database())
        repository = Repository("pacman", "extra", (mirror.as_uri(),))
        index = self.index([repository])
        index.refresh(["pacman"])
        mirror.write_bytes(b"broken")
        results, failures = index.refresh(["pacman"])
        self.assertFalse(results)
        self.assertTrue(failures)
        self.assertEqual(index.search("firefox", ["pacman"])[0][0].version, "130.0-1")

    def test_removed_repository_is_not_searched(self):
        self.cache.replace(Repository("pacman", "removed", ()),
                           [Package("firefox", "pacman", repository="removed")])
        self.assertEqual(self.index([]).search("firefox", ["pacman"])[0], [])

    def test_dnf_repomd_primary_location_and_checksum(self):
        primary = gzip.compress(PRIMARY)
        self.write("rpm/repodata/arbitrary-primary.xml.gz", primary)
        repomd = f'''<repomd xmlns="http://linux.duke.edu/metadata/repo">
<data type="primary"><location href="repodata/arbitrary-primary.xml.gz"/>
<checksum type="sha256">{hashlib.sha256(primary).hexdigest()}</checksum></data>
<data type="modules"/></repomd>'''
        self.write("rpm/repodata/repomd.xml", repomd)
        repository = Repository("dnf", "main", ((self.root / "rpm").as_uri(),), "x86_64")
        index = self.index([repository])
        self.assertEqual(index.refresh_repository(repository), 1)
        self.assertTrue(any("模块流" in message for message in self.messages))
        self.write("rpm/repodata/arbitrary-primary.xml.gz", gzip.compress(PRIMARY + b" "))
        with self.assertRaisesRegex(DickError, "校验失败"):
            index.refresh_repository(repository)
        self.assertEqual(self.cache.search("firefox", ["dnf"])[0].version, "1:130.0-2")

    def test_aur_rpc_url_cache_and_error(self):
        client = Mock()
        client.get.return_value = json.dumps({"type": "search", "results": [
            {"Name": "firefox", "Version": "130", "Description": "Browser"}
        ]}).encode()
        index = self.index([Repository("aur", "aur", ())], client)
        self.assertEqual(index.search("firefox", ["aur"])[0][0].source, "aur")
        index.search("firefox", ["aur"])
        client.get.assert_called_once_with("https://aur.archlinux.org/rpc/v5/search/firefox?by=name-desc")
        index.search("firefox", ["aur"], exact=True)
        self.assertEqual(client.get.call_args.args[0], "https://aur.archlinux.org/rpc/v5/info?arg%5B%5D=firefox")
        client.get.return_value = b'{"type":"error","error":"Too many results"}'
        packages, errors = index.search("other", ["aur"])
        self.assertFalse(packages)
        self.assertIn("Too many results", errors[0])

    def test_flatpak_json_index_survives_the_dropped_columns(self):
        """flatpak ≥ 1.18 的 remote-ls 会静默丢掉 description/version 列：必须改读 --json。"""
        repository = Repository("flatpak", "flathub", ())
        index = self.index([repository])
        payload = json.dumps([
            {"name": "Firefox", "application_id": "org.mozilla.firefox", "version": "",
             "branch": "stable", "origin": "flathub"},
            {"name": "", "application_id": "org.gnome.Calculator", "version": "48.1",
             "branch": "stable", "origin": "flathub"},
        ])
        with patch("dick.index.native_output", return_value=payload) as output:
            packages, errors = index.search("firefox", ["flatpak"], exact=True)
        self.assertFalse(errors)
        self.assertEqual([package.name for package in packages], ["org.mozilla.firefox"])
        self.assertEqual(packages[0].description, "Firefox")          # 没有描述时退回显示名
        self.assertEqual(output.call_args.args[0], ["flatpak", "remote-ls", "--app", "--json", "flathub"])
        self.assertEqual(output.call_args.kwargs["env"]["LC_ALL"], "C")  # 否则 JSON 键会被翻译

    def test_flatpak_without_json_falls_back_to_tsv(self):
        repository = Repository("flatpak", "flathub", ())
        index = self.index([repository])
        calls = []

        def fake(command, timeout=60, env=None):
            calls.append(command)
            if "--json" in command:
                raise DickError("命令失败 flatpak remote-ls：error: Unknown option --json")
            return "org.mozilla.firefox\tFirefox\tstable\tflathub\n"

        with patch("dick.index.native_output", side_effect=fake):
            packages, errors = index.search("firefox", ["flatpak"], exact=True)
        self.assertFalse(errors)
        self.assertEqual(packages[0].name, "org.mozilla.firefox")
        self.assertEqual(packages[0].description, "Firefox")
        self.assertEqual(calls[1], ["flatpak", "remote-ls", "--app",
                                    "--columns=application,name,branch,origin", "flathub"])

    def test_flatpak_garbage_line_names_the_offending_output(self):
        index = self.index([Repository("flatpak", "flathub", ())])

        def fake(command, timeout=60, env=None):
            if "--json" in command:
                return "不是 JSON"
            return "No matches found\n"

        with patch("dick.index.native_output", side_effect=fake):
            packages, errors = index.search("firefox", ["flatpak"], exact=True)
        self.assertFalse(packages)
        self.assertIn("Flatpak 输出不是预期的 TSV：No matches found", errors[0])

    def test_snap_native_table_is_cached(self):
        index = self.index([Repository("snap", "snap-store", ())])
        with patch("dick.index.native_output", return_value="Name Version Publisher Notes Summary\nfirefox 130 mozilla - Web browser\n") as output:
            packages, errors = index.search("firefox", ["snap"], exact=True)
            index.search("firefox", ["snap"], exact=True)
        self.assertFalse(errors)
        self.assertEqual(packages[0].description, "Web browser")
        output.assert_called_once_with(["snap", "find", "firefox"])

    def test_linyaps_json_search_is_cached_and_lists_every_repository(self):
        payload = json.dumps({"stable": [
            {"id": "org.deepin.calculator", "name": "deepin-calculator", "version": "6.5.26.1",
             "channel": "main", "packageInfoV2Module": "binary", "arch": ["x86_64"],
             "description": "Calculator for UOS"},
        ], "nightly": [
            {"id": "org.gnome.calculator", "name": "org.gnome.calculator", "version": "48.1.0.0",
             "arch": [], "description": None},
        ]})
        index = self.index([Repository("linyaps", "stable", ())])
        with patch("dick.index.native_output", return_value=payload) as output:
            packages, errors = index.search("calculator", ["linyaps"], exact=True)
            index.search("calculator", ["linyaps"], exact=True)
        self.assertFalse(errors)
        self.assertEqual([package.repository for package in packages], ["stable", "nightly"])
        self.assertEqual([package.name for package in packages],
                         ["org.deepin.calculator", "org.gnome.calculator"])
        self.assertEqual(packages[0].version, "6.5.26.1")
        self.assertEqual(packages[0].description, "Calculator for UOS")
        self.assertEqual(packages[0].architecture, "x86_64")
        self.assertEqual(packages[1].description, "")
        output.assert_called_once_with(["ll-cli", "--json", "search", "calculator"], timeout=60)

    def test_linyaps_exact_match_accepts_id_segment_and_display_name(self):
        payload = json.dumps({"stable": [
            {"id": "org.deepin.calculator", "name": "deepin-calculator", "version": "6.5.26.1"},
            {"id": "org.gnome.calculator", "name": "org.gnome.calculator", "version": "48.1.0.0"},
        ]})
        index = self.index([Repository("linyaps", "stable", ())])
        with patch("dick.index.native_output", return_value=payload):
            self.assertEqual([package.name for package in index.search("calculator", ["linyaps"], exact=True)[0]],
                             ["org.deepin.calculator", "org.gnome.calculator"])
            self.assertEqual([package.name for package in index.search("deepin-calculator", ["linyaps"], exact=True)[0]],
                             ["org.deepin.calculator"])
            self.assertEqual([package.name for package in index.search("org.gnome.calculator", ["linyaps"], exact=True)[0]],
                             ["org.gnome.calculator"])
            self.assertEqual(len(index.search("calculator", ["linyaps"])[0]), 2)

    def test_linyaps_invalid_output_is_reported(self):
        index = self.index([Repository("linyaps", "stable", ())])
        for payload in ("ll-cli: command not found", json.dumps({"stable": [{"name": "broken"}]}),
                        json.dumps(12)):
            with self.subTest(payload=payload), patch("dick.index.native_output", return_value=payload):
                packages, errors = index.search("calculator", ["linyaps"])
            self.assertFalse(packages)
            self.assertTrue(errors)

    def test_linyaps_refresh_only_clears_the_query_cache(self):
        packages = [Package("org.deepin.calculator", "linyaps", "Calculator", "6.5.26.1", "stable")]
        self.cache.query_put("linyaps", "search:calculator", packages)
        index = self.index([Repository("linyaps", "stable", ())])
        results, failures = index.refresh(["linyaps"])
        self.assertFalse(failures)
        self.assertEqual(results, [{"source": "linyaps", "repository": "stable", "on_demand": True}])
        self.assertIsNone(self.cache.query_get("linyaps", "search:calculator", 10000))
        self.assertIsNone(self.cache.snapshot(Repository("linyaps", "stable", ())))


    def test_guix_search_parses_table_and_filters_exact_names(self):
        """guix -A 的输出是四列表格（name version outputs location），没有描述列。"""
        table = ("firefox  128.0  out  gnu/packages/gnuzilla.scm:123:2\n"
                 "firefox-esr  115.14.0  out  gnu/packages/gnuzilla.scm:456:2\n")
        index = self.index([Repository("guix", "guix", ())])
        with patch("dick.index.native_output", return_value=table) as output:
            packages, errors = index.search("firefox", ["guix"])
            index.search("firefox", ["guix"])  # 第二次走查询缓存
            exact, _ = index.search("firefox", ["guix"], exact=True)
        self.assertFalse(errors)
        self.assertEqual([package.name for package in packages], ["firefox", "firefox-esr"])
        self.assertEqual([package.name for package in exact], ["firefox"])
        self.assertEqual((packages[0].source, packages[0].version, packages[0].description),
                         ("guix", "128.0", ""))
        self.assertEqual(output.call_count, 2)
        output.assert_any_call(["guix", "package", "-A", "firefox"], timeout=180)

    def test_guix_and_nixpkgs_output_problems_are_reported(self):
        index = self.index([Repository("guix", "guix", ()), Repository("nixpkgs", "nixpkgs", ())])
        with patch("dick.index.native_output", return_value="firefox\n"):
            packages, errors = index.search("firefox", ["guix"])
        self.assertFalse(packages)
        self.assertIn("四列表格", "；".join(errors))
        with patch("dick.index.native_output", side_effect=DickError(
                "命令失败 nix search：experimental Nix feature 'nix-command' is disabled")):
            packages, errors = index.search("firefox", ["nixpkgs"])
        self.assertFalse(packages)
        self.assertIn("experimental-features", "；".join(errors))
        with patch("dick.index.native_output", return_value="not json"):
            packages, errors = index.search("firefox", ["nixpkgs"])
        self.assertFalse(packages)
        self.assertIn("JSON", "；".join(errors))

    def test_nixpkgs_search_reads_json_and_strips_attribute_prefixes(self):
        payload = json.dumps({
            "legacyPackages.x86_64-linux.firefox": {"pname": "firefox", "version": "128.0",
                                                    "description": "Web browser"},
            "legacyPackages.x86_64-linux.python3Packages.requests": {"version": "2.32.3"},
        })
        index = self.index([Repository("nixpkgs", "nixpkgs", ())])
        with patch("dick.index.native_output", return_value=payload) as output:
            packages, errors = index.search("firefox", ["nixpkgs"])
        self.assertFalse(errors)
        self.assertEqual([(package.name, package.version, package.description) for package in packages],
                         [("firefox", "128.0", "Web browser"), ("python3Packages.requests", "2.32.3", "")])
        self.assertTrue(all(package.source == "nixpkgs" for package in packages))
        output.assert_called_once_with(["nix", "search", "--json", "nixpkgs", "firefox"], timeout=300)

    def test_guix_and_nixpkgs_expressions_are_escaped(self):
        """`+`/`(` 这类元字符在 guix 的 POSIX 正则和 nix 的 std::regex 里都是语法，必须当字面量。"""
        index = self.index([Repository("guix", "guix", ()), Repository("nixpkgs", "nixpkgs", ())])
        with patch("dick.index.native_output", return_value="") as output:
            index.search("g++", ["guix"])
            index.search("g++", ["nixpkgs"])
        self.assertEqual([call.args[0][-1] for call in output.call_args_list], ["g\\+\\+", "g\\+\\+"])


    def test_query_cache_ttl_is_per_source(self):
        """按需来源各用各的缓存有效期：guix 一天，aur 等仍跟全局 ttl 走。"""
        index = self.index([Repository("guix", "guix", ()), Repository("aur", "aur", ())])
        with patch.object(index.cache, "query_get", return_value=None) as query_get, \
                patch("dick.index.native_output", return_value=""):
            index.search("firefox", ["guix"])
            index.search("firefox", ["aur"])
        ttls = {call.args[0]: call.args[2] for call in query_get.call_args_list}
        self.assertEqual(ttls["guix"], 86400)
        self.assertEqual(ttls["aur"], index.settings.ttl)
        index.settings.query_ttls["guix"] = 60  # [cache.query_ttl] 的覆盖值要真的传到缓存
        with patch.object(index.cache, "query_get", return_value=None) as query_get, \
                patch("dick.index.native_output", return_value=""):
            index.search("firefox", ["guix"])
        self.assertEqual(query_get.call_args.args[2], 60)


class ConfigTests(FixtureTest):
    def test_sources_default_to_all_enabled(self):
        self.assertEqual(self.settings.enabled_sources, SOURCES)
        self.assertTrue(all(self.settings.enabled(source) for source in SOURCES))

    def test_set_enabled_creates_file_and_keeps_other_content(self):
        config = self.write("nested/dick.toml", "# 我的注释\n[syntax]\nprefer = \"pacman\"\n\n[sources]\n# 说明\nenabled = [\"pacman\", \"aur\"]\n\n[network]\nworkers = 2\n")
        settings = Settings(config, self.root, self.root / "other-cache")
        settings.set_enabled(["pacman", "apt", "snap"])
        text = config.read_text(encoding="utf-8")
        self.assertIn("# 我的注释", text)
        self.assertIn('prefer = "pacman"', text)
        self.assertIn("workers = 2", text)
        self.assertIn('enabled = ["pacman", "apt", "snap"]', text)
        self.assertEqual(text.count("enabled"), 1)
        self.assertEqual(Settings(config, self.root, self.root / "other-cache").enabled_sources,
                         ("pacman", "apt", "snap"))

    def test_set_enabled_appends_missing_section(self):
        config = self.write("dick.toml", "[syntax]\nprefer = \"auto\"\n")
        Settings(config, self.root, self.root / "other-cache").set_enabled(["dnf"])
        self.assertEqual(config.read_text(encoding="utf-8"),
                         "[syntax]\nprefer = \"auto\"\n\n[sources]\nenabled = [\"dnf\"]\n")

    def test_missing_explicit_config_only_tolerated_when_creating(self):
        missing = self.root / "absent.toml"
        with self.assertRaisesRegex(DickError, "配置文件不存在"):
            Settings(missing, self.root, self.root / "other-cache")
        settings = Settings(missing, self.root, self.root / "other-cache", create=True)
        self.assertEqual(settings.enabled_sources, SOURCES)
        settings.set_enabled(["linyaps"])
        self.assertEqual(missing.read_text(encoding="utf-8"), "[sources]\nenabled = [\"linyaps\"]\n")

    def test_snap_is_forced_to_the_last_place(self):
        """snap 是兜底来源：配置里排第一、甚至漏写，最终都必须垫底。"""
        first = self.write("snap-first.toml", '[priority.arch]\norder = ["snap", "pacman", "aur"]\n')
        self.assertEqual(Settings(first, self.root, self.root / "c1").priority,
                         ["pacman", "aur", "snap"])
        missing = self.write("snap-missing.toml", '[priority.arch]\norder = ["pacman"]\n')
        self.assertEqual(Settings(missing, self.root, self.root / "c2").priority, ["pacman", "snap"])
        only = self.write("snap-only.toml", '[priority.arch]\norder = ["snap"]\n')
        self.assertEqual(Settings(only, self.root, self.root / "c3").priority, ["snap"])
        duplicate = self.write("snap-twice.toml", '[priority.arch]\norder = ["pacman", "snap", "snap"]\n')
        with self.assertRaisesRegex(DickError, "不能包含重复来源"):
            Settings(duplicate, self.root, self.root / "c4")

    def test_alpine_family_prefers_apk_and_knows_the_new_sources(self):
        self.write("etc/os-release", "ID=alpine\n")
        settings = Settings(self.config, self.root, self.root / "alpine-cache", create=True)
        self.assertEqual(settings.family, "alpine")
        self.assertEqual(settings.priority[0], "apk")
        self.assertEqual(settings.priority[-1], LAST_SOURCE)
        self.assertLess(settings.priority.index("guix"), settings.priority.index(LAST_SOURCE))
        with patch("dick.config.shutil.which",
                   side_effect=lambda command: "/usr/sbin/apk" if command == "apk" else None):
            self.assertTrue(settings.available("apk"))
            self.assertFalse(settings.available("guix"))
            self.assertFalse(settings.available("nixpkgs"))
        with patch("dick.config.shutil.which",
                   side_effect=lambda command: "/usr/bin/nix" if command == "nix" else None):
            self.assertTrue(settings.available("nixpkgs"))

    def test_query_cache_ttl_defaults_and_overrides(self):
        """guix/nixpkgs 每查一次都要现跑慢速原生命令，所以查询缓存默认给一天而不是 15 分钟。"""
        settings = Settings(self.config, self.root, self.root / "ttl-cache", create=True)
        self.assertEqual(settings.ttl, 900)
        self.assertEqual(settings.query_ttl("guix"), 86400)
        self.assertEqual(settings.query_ttl("nixpkgs"), 86400)
        self.assertEqual(settings.query_ttl("aur"), 900)
        custom = self.write("query-ttl.toml",
                            "[cache]\nttl = 60\n[cache.query_ttl]\nguix = 7200\nsnap = 0\n")
        tuned = Settings(custom, self.root, self.root / "ttl-cache-2", create=True)
        self.assertEqual(tuned.query_ttl("guix"), 7200)
        self.assertEqual(tuned.query_ttl("snap"), 0)
        self.assertEqual(tuned.query_ttl("nixpkgs"), 86400)  # 没写的来源保留默认
        self.assertEqual(tuned.query_ttl("linyaps"), 60)     # 其余按需来源跟全局 ttl
        for broken in ('[cache.query_ttl]\nfirefox = 60\n',
                       '[cache.query_ttl]\nguix = "一天"\n',
                       '[cache.query_ttl]\nguix = -1\n'):
            with self.subTest(broken=broken):
                with self.assertRaises(DickError):
                    Settings(self.write("broken-ttl.toml", broken), self.root,
                             self.root / "ttl-cache-3", create=True)

    def test_every_family_default_priority_ends_with_snap(self):
        for family, order in PRIORITIES.items():
            with self.subTest(family=family):
                self.assertEqual(order[-1], LAST_SOURCE)
                self.assertEqual(len(set(order)), len(order))
                self.assertTrue(set(order) <= set(SOURCES))

    def test_invalid_enabled_list_is_rejected(self):
        for content in ('[sources]\nenabled = "pacman"\n', '[sources]\nenabled = ["nope"]\n',
                        '[sources]\nenabled = [1]\n'):
            with self.subTest(content=content):
                config = self.write("bad-sources.toml", content)
                with self.assertRaisesRegex(DickError, "sources.enabled"):
                    Settings(config, self.root, self.root / "other-cache")


class LocalTests(unittest.TestCase):
    def test_pacman_and_aur_use_separate_native_lists(self):
        self.assertEqual(LIST_COMMANDS["pacman"], ["pacman", "-Qn"])
        self.assertEqual(LIST_COMMANDS["aur"], ["pacman", "-Qm"])
        output = "firefox 130.0-1\nhtop 3.3.0-1\n"
        self.assertEqual([(package.source, package.name, package.version)
                          for package in PARSERS["pacman"](output)],
                         [("pacman", "firefox", "130.0-1"), ("pacman", "htop", "3.3.0-1")])
        self.assertEqual(PARSERS["aur"]("foreign 1.0-1\n")[0].source, "aur")

    def test_dpkg_only_reports_installed_records(self):
        output = ("firefox\t130.0\tii \tWeb browser\n"
                  "removed\t1.0\trc \tRemoved package\n"
                  "half\t1.0\tiHR\tHalf installed\n"
                  "broken\n")
        packages = PARSERS["apt"](output)
        self.assertEqual([(package.name, package.version, package.description) for package in packages],
                         [("firefox", "130.0", "Web browser"), ("half", "1.0", "Half installed")])

    def test_rpm_and_flatpak_and_snap(self):
        self.assertEqual([(package.name, package.version, package.description)
                          for package in PARSERS["dnf"]("firefox\t130.0-1\tWeb browser\n")],
                         [("firefox", "130.0-1", "Web browser")])
        self.assertEqual([(package.name, package.version)
                          for package in PARSERS["flatpak"]("org.mozilla.firefox\t130.0\n")],
                         [("org.mozilla.firefox", "130.0")])
        output = ("Name Version Rev Tracking Publisher Notes\n"
                  "firefox 130 1 latest/stable mozilla -\n"
                  "No snaps are installed yet.\n")
        self.assertEqual([(package.name, package.version) for package in PARSERS["snap"](output)],
                         [("firefox", "130")])

    def test_linyaps_list_keeps_only_apps(self):
        payload = json.dumps([
            {"id": "cn.wps.wps-office", "version": "12.1.2", "kind": "app", "description": "WPS"},
            {"id": "org.deepin.Runtime", "version": "1.0", "kind": "runtime"},
        ])
        self.assertEqual([(package.name, package.version, package.description)
                          for package in PARSERS["linyaps"](payload)],
                         [("cn.wps.wps-office", "12.1.2", "WPS")])
        for broken in ("not json", json.dumps({"stable": []}), json.dumps([{"version": "1"}])):
            with self.subTest(broken=broken), self.assertRaises(DickError):
                PARSERS["linyaps"](broken)

    def test_read_installed_tolerates_empty_aur_and_reports_failures(self):
        settings = Mock(timeout=20)
        with patch("dick.local.native_output", side_effect=DickError("pacman 退出码 1")) as output:
            self.assertEqual(read_installed(settings, "aur"), [])
            with self.assertRaises(DickError):
                read_installed(settings, "pacman")
        output.assert_any_call(["pacman", "-Qm"], timeout=60)

    def test_apk_installed_database_is_read_from_the_root(self):
        """apk 的已安装清单是 /lib/apk/db/installed 文件，字段和 APKINDEX 一样，读文件也天然支持 --root。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "lib/apk/db/installed"
            path.parent.mkdir(parents=True)
            path.write_text("C:Q1MQxXAVMr80Ty95MYYNhPNBn/WDs=\nP:firefox\nV:128.0-r1\nA:x86_64\n"
                            "T:Web browser\n\nP:musl\nV:1.2.5-r0\nT:C library\n", encoding="utf-8")
            self.assertEqual(FILE_LISTS["apk"][0], "lib/apk/db/installed")
            self.assertEqual([(package.name, package.version, package.description)
                              for package in read_installed(Mock(timeout=20, root=root), "apk")],
                             [("firefox", "128.0-r1", "Web browser"), ("musl", "1.2.5-r0", "C library")])
            self.assertEqual(read_installed(Mock(timeout=20, root=root / "absent"), "apk"), [])

    def test_guix_and_nixpkgs_installed_lists(self):
        self.assertEqual(LIST_COMMANDS["guix"], ["guix", "package", "--list-installed"])
        self.assertEqual(LIST_COMMANDS["nixpkgs"], ["nix", "profile", "list", "--json"])
        self.assertEqual([(package.name, package.version) for package in PARSERS["guix"](
            "firefox  128.0  out  gnu/packages/gnuzilla.scm:123:2\n")], [("firefox", "128.0")])
        with self.assertRaisesRegex(DickError, "四列表格"):
            PARSERS["guix"]("firefox\n")
        payload = json.dumps({"elements": {
            "0": {"attrPath": "legacyPackages.x86_64-linux.firefox", "version": "128.0",
                  "description": "Web browser"},
            "firefox-esr": "legacyPackages.x86_64-linux.firefox-esr",
        }})
        self.assertEqual([(package.name, package.version) for package in PARSERS["nixpkgs"](payload)],
                         [("firefox", "128.0"), ("firefox-esr", "")])
        self.assertEqual(PARSERS["nixpkgs"](json.dumps({"elements": {}})), [])
        for broken in ("not json", json.dumps([1])):
            with self.subTest(broken=broken), self.assertRaises(DickError):
                PARSERS["nixpkgs"](broken)

    def test_matching_prefers_exact_names_and_id_segments(self):
        exact = LocalPackage("pacman", "firefox")
        segment = LocalPackage("flatpak", "org.mozilla.firefox")
        prefixed = LocalPackage("pacman", "firefox-esr")
        self.assertTrue(matches(exact, "firefox"))
        self.assertTrue(all(matches(package, "firefox") for package in (exact, segment, prefixed)))
        self.assertTrue(rank(exact, "fire") > 0)
        self.assertTrue(matches(exact, "fire"))
        self.assertFalse(matches(exact, "chromium"))
        self.assertEqual([rank(package, "firefox") for package in (exact, segment, prefixed)], [0, 1, 2])


class SyntaxTests(unittest.TestCase):
    def test_command_tree(self):
        cases = {
            ("source", "list"): "source_list",
            ("source", "scan"): "source_scan",
            ("source", "enable", "flatpak"): "source_enable",
            ("source", "disable", "aur", "snap"): "source_disable",
            ("install", "firefox"): "install",
            ("remove", "firefox"): "remove",
            ("list",): "list",
            ("list", "python"): "list",
            ("search", "web", "browser"): "search",
            ("update",): "update",
            ("upgrade",): "upgrade",
            ("updateme",): "updateme",
            ("removeme",): "removeme",
        }
        for arguments, name in cases.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(normalize(list(arguments)).name, name)

    def test_targets_and_joined_keywords(self):
        self.assertEqual(normalize(["install", "firefox", "htop"]).targets, ("firefox", "htop"))
        self.assertEqual(normalize(["search", "web", "browser"]).targets, ("web browser",))
        self.assertEqual(normalize(["list", "python", "3"]).targets, ("python 3",))
        self.assertEqual(normalize(["update"]).targets, ())

    def test_missing_targets_and_removed_commands_are_rejected(self):
        removed = ["-S", "-Ss", "-Si", "-R", "-Rns", "-Sy", "-Syu", "-Qs", "sources", "refresh",
                   "info", "remove-deep", "full-upgrade", "search-local", "frobnicate"]
        for arguments in (["install"], ["remove"], ["search"], ["update", "firefox"], ["upgrade", "firefox"],
                          ["updateme", "firefox"], ["removeme", "firefox"],
                          ["source"], ["source", "enable"], ["source", "disable"], ["source", "reset"],
                          ["source", "list", "extra"],
                          *([command, "firefox"] for command in removed)):
            with self.subTest(arguments=arguments), self.assertRaises(DickError):
                normalize(arguments)

    def test_unknown_sources_and_dashed_targets(self):
        with self.assertRaisesRegex(DickError, "未知来源"):
            normalize(["source", "enable", "pip"])
        with self.assertRaisesRegex(DickError, "不能为空"):
            normalize(["install", "--unsafe"])

    def test_global_options_and_unknown_flags(self):
        args, remaining = options(["--dry-run", "--source", "pacman", "--yes", "--exact", "--deep",
                                   "install", "firefox"])
        self.assertTrue(args.dry_run and args.yes and args.exact and args.deep)
        self.assertEqual(args.source, ["pacman"])
        self.assertEqual(normalize(remaining).targets, ("firefox",))
        with self.assertRaises(DickError):
            normalize([*remaining, "--unsafe"])

    def test_self_management_options(self):
        args, remaining = options(["--prefix", "/tmp/x", "--ref", "v0.2", "--purge", "removeme", "--yes"])
        self.assertEqual((args.prefix, args.ref), ("/tmp/x", "v0.2"))
        self.assertTrue(args.purge and args.yes)
        self.assertEqual(normalize(remaining), Action("removeme"))

    def test_jobs_and_limit_options(self):
        args, remaining = options(["--jobs", "3", "--limit", "7", "update"])
        self.assertEqual((args.jobs, args.limit), (3, 7))
        self.assertEqual(remaining, ["update"])
        for arguments in (["--jobs", "0", "update"], ["--limit", "0", "list"]):
            with self.assertRaises(DickError):
                options(arguments)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            options(["--source", "pip", "list"])

    def test_linyaps_source_option_executable_and_default_priority(self):
        args, remaining = options(["--source", "linyaps", "search", "calculator"])
        self.assertEqual(args.source, ["linyaps"])
        self.assertEqual(remaining, ["search", "calculator"])
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(cache_dir=Path(directory))
            self.assertIn("linyaps", settings.priority)
            with patch("dick.config.shutil.which",
                       side_effect=lambda command: "/usr/bin/ll-cli" if command == "ll-cli" else None):
                self.assertTrue(settings.available("linyaps"))
                self.assertFalse(settings.available("apt"))


class ProgressTests(unittest.TestCase):
    def test_manager_styles_render_one_progress_bar(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        for family, marker in (("arch", "Synchronizing package databases"),
                               ("debian", "Get:"), ("fedora", "Refreshing repository metadata")):
            stream = TTY()
            progress = Progress(family, 2, stream)
            progress.start()
            progress.update("core")
            progress.update("extra")
            output = stream.getvalue()
            self.assertIn(marker, output)
            self.assertIn("100%", output)


class InstallationTests(FixtureTest):
    def installer(self, index, dry_run=False):
        self.settings.root = Path("/")
        installer = Installer(self.settings, index, self.messages.append, dry_run=dry_run)
        installer.privileged = lambda command: command
        return installer

    def test_native_failure_falls_back_to_flatpak(self):
        index = Mock()
        index.search.side_effect = [([Package("firefox", "pacman", repository="extra")], []),
                                    ([], []), ([Package("org.mozilla.firefox", "flatpak", repository="flathub")], [])]
        installer = self.installer(index)
        with patch.object(self.settings, "available", return_value=True), \
                patch("dick.install.subprocess.run", side_effect=[subprocess.CompletedProcess([], 1),
                                                                   subprocess.CompletedProcess([], 0)]) as run:
            result = installer.install("firefox", ["pacman", "aur", "flatpak"])
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "flatpak")
        self.assertEqual(run.call_args_list[0].args[0], ["pacman", "-S", "--", "extra/firefox"])
        self.assertEqual(run.call_args_list[1].args[0], ["flatpak", "install", "--", "flathub", "org.mozilla.firefox"])

    def test_dry_run_does_not_execute(self):
        index = Mock()
        index.search.return_value = ([Package("firefox", "pacman", repository="extra")], [])
        installer = self.installer(index, dry_run=True)
        with patch.object(self.settings, "available", return_value=True), patch("dick.install.subprocess.run") as run:
            self.assertTrue(installer.install("firefox", ["pacman"])["success"])
        run.assert_not_called()

    def test_cancellation_does_not_fall_back(self):
        index = Mock()
        index.search.return_value = ([Package("firefox", "pacman", repository="extra")], [])
        installer = self.installer(index)
        with patch.object(self.settings, "available", return_value=True), \
                patch("dick.install.subprocess.run", return_value=subprocess.CompletedProcess([], 130)) as run:
            with self.assertRaises(KeyboardInterrupt):
                installer.install("firefox", ["pacman", "aur", "flatpak"])
        self.assertEqual(run.call_count, 1)

    def test_ambiguous_flatpak_alias_requires_full_id(self):
        index = Mock()
        index.search.return_value = ([Package("org.one.firefox", "flatpak", repository="flathub"),
                                      Package("org.two.firefox", "flatpak", repository="flathub")], [])
        installer = self.installer(index)
        with patch.object(self.settings, "available", return_value=True), patch("dick.install.subprocess.run") as run:
            result = installer.install("firefox", ["flatpak"])
        self.assertFalse(result["success"])
        self.assertIn("完整 ID", result["attempts"][0]["error"])
        run.assert_not_called()

    def test_aur_helper_is_not_prefixed_with_sudo(self):
        installer = self.installer(Mock(), dry_run=True)
        with patch("dick.install.shutil.which", side_effect=lambda command: "/usr/bin/paru" if command == "paru" else None):
            self.assertEqual(installer.install_command(Package("firefox", "aur", repository="aur")),
                             ["paru", "-S", "--", "aur/firefox"])

    def test_linyaps_install_and_remove_go_through_sudo_for_polkit(self):
        """玲珑的服务用 polkit（默认 auth_admin）把关，普通用户调用只会在桌面会话里弹认证框；
        网页/手机没人守着桌面，所以安装与卸载都必须和 pacman 一样走 sudo。"""
        self.settings.root = Path("/")
        installer = Installer(self.settings, Mock(), self.messages.append, yes=True)
        installer.privileged = lambda command: ["sudo", *command]
        self.assertEqual(installer.install_command(Package("org.deepin.calculator", "linyaps", repository="stable")),
                         ["sudo", "ll-cli", "install", "-y", "org.deepin.calculator"])
        self.assertEqual(installer.uninstall_command(Package("cn.wps.wps-office", "linyaps")),
                         ["sudo", "ll-cli", "uninstall", "cn.wps.wps-office"])

    def test_linyaps_not_authorized_explains_polkit(self):
        """`Error 9: not authorized` 不能只留一句「退出码 255」，要说清是 polkit 授权问题。"""
        index = Mock()
        index.search.side_effect = lambda *args, **kwargs: (
            [Package("com.qq.music", "linyaps", repository="stable")], [])
        installer = self.installer(index)

        def fail(command):
            installer.last_output = ["执行：sudo ll-cli install -y com.qq.music",
                                     "Error 9: not authorized"]
            return 255

        with patch.object(self.settings, "available", return_value=True), \
                patch.object(installer, "execute", side_effect=fail):
            result = installer.install("com.qq.music", ["linyaps"])
        self.assertFalse(result["success"])
        joined = "\n".join(self.messages)
        self.assertIn("polkit", joined)
        self.assertIn("sudo ll-cli install -y com.qq.music", joined)
        self.assertIn("49-dick-linglong.rules", joined)

    def test_running_app_uninstall_failure_is_explained(self):
        """ll-cli 拒绝卸载正在运行的应用：报告要带原因，并提示用 ll-cli kill 结束它。"""
        installer = self.installer(Mock())
        installer.last_output = [
            "执行：sudo ll-cli uninstall com.qq.music",
            "The application is currently running and cannot be uninstalled. "
            "Please turn off the application and try again."]
        self.assertTrue(installer.looks_like_running_app())
        self.assertEqual(installer.failure_reason(255),
                         "The application is currently running and cannot be uninstalled. "
                         "Please turn off the application and try again.")
        self.assertTrue(installer.report_uninstall_hint(LocalPackage("linyaps", "com.qq.music")))
        joined = "\n".join(self.messages)
        self.assertIn("正在运行", joined)
        self.assertIn("ll-cli kill com.qq.music", joined)
        # 「in use」在别家输出里另有含义，不该套上玲珑的提示
        self.assertFalse(installer.report_uninstall_hint(LocalPackage("apt", "com.qq.music")))
        # 认不出原因时不要瞎猜，退回退出码
        installer.last_output = ["执行：sudo ll-cli uninstall com.qq.music"]
        self.assertFalse(installer.looks_like_running_app())
        self.assertFalse(installer.report_uninstall_hint(LocalPackage("linyaps", "com.qq.music")))
        self.assertEqual(installer.failure_reason(255), "退出码 255")

    def test_apk_guix_and_nixpkgs_install_and_remove_commands(self):
        """apk 装到系统里；guix / nixpkgs 装进用户自己的 profile（所以这三个来源不加 sudo）。"""
        installer = self.installer(Mock(), dry_run=True)
        self.assertEqual(installer.install_command(Package("firefox", "apk", repository="v3.20/main")),
                         ["apk", "add", "--no-cache", "firefox"])
        self.assertEqual(installer.install_command(Package("firefox", "guix")),
                         ["guix", "install", "firefox"])
        self.assertEqual(installer.install_command(Package("python3Packages.requests", "nixpkgs")),
                         ["nix", "profile", "install", "nixpkgs#python3Packages.requests"])
        self.assertEqual(installer.uninstall_command(Package("firefox", "apk")),
                         ["apk", "del", "firefox"])
        self.assertEqual(installer.uninstall_command(Package("firefox", "guix")),
                         ["guix", "remove", "firefox"])
        self.assertEqual(installer.uninstall_command(Package("firefox", "nixpkgs")),
                         ["nix", "profile", "remove", "firefox"])

    def test_apk_is_privileged_and_alpine_upgrades_through_it(self):
        self.settings.family = "alpine"
        self.settings.root = Path("/")
        installer = Installer(self.settings, Mock(), self.messages.append, yes=True)
        installer.privileged = lambda command: ["sudo", *command]
        self.assertEqual(installer.install_command(Package("firefox", "apk")),
                         ["sudo", "apk", "add", "--no-cache", "firefox"])
        self.assertEqual(installer.uninstall_command(Package("firefox", "apk")),
                         ["sudo", "apk", "del", "firefox"])
        with patch.object(self.settings, "available", return_value=True):
            self.assertEqual(installer.native_source(["pacman", "apk"]), "apk")
            self.assertEqual(installer.upgrade_command(["apk"]), ["sudo", "apk", "-U", "upgrade"])
            self.assertEqual(installer.refresh_command("apk"), ["sudo", "apk", "update"])
            with self.assertRaisesRegex(DickError, "pacman/apt/dnf/apk"):
                installer.native_source(["guix", "nixpkgs"])

    def test_stream_captures_output_and_returncode(self):
        """Web 任务：子进程的 stdout/stderr 与回车刷新的进度都要进任务日志，退出码原样返回。"""
        self.settings.root = Path("/")
        script = self.write("noisy.sh", "printf '下载 10%%\\r下载 100%%\\n'\n"
                                        "echo '错误：目标未找到' 1>&2\n"
                                        "printf '没有换行的尾巴'\nexit 7\n")
        lines = []
        installer = Installer(self.settings, Mock(), lines.append, stream=lines.append)
        self.assertEqual(installer.execute(["/bin/sh", str(script)]), 7)
        for expected in ("下载 10%", "下载 100%", "错误：目标未找到", "没有换行的尾巴"):
            self.assertIn(expected, lines)

    def test_cli_stream_keeps_stdout_clean_but_merges_stderr(self):
        """--json 模式下子进程输出进 stream，stdout 只留给 JSON。"""
        self.settings.root = Path("/")
        sink = self.root / "stream.txt"
        with sink.open("wb") as handle:
            installer = Installer(self.settings, Mock(), self.messages.append, stream=handle)
            self.assertEqual(installer.execute(["/bin/sh", "-c", "echo 正常; echo 错误 1>&2; exit 4"]), 4)
        self.assertEqual(sink.read_text(encoding="utf-8").split(), ["正常", "错误"])

    def test_stream_reports_missing_sudo_instead_of_failing_silently(self):
        """没有终端时 sudo 要密码：日志必须说清原因，而且不能真去跑那条命令。"""
        self.settings.root = Path("/")
        marker = self.root / "不该被创建"
        lines = []
        installer = Installer(self.settings, Mock(), lines.append, stream=lines.append)
        installer.sudo_ready = lambda: False
        with patch("dick.install.sys.stdin", io.StringIO()):  # 没有 tty，和 Web 任务一样
            self.assertEqual(installer.execute(["sudo", "touch", str(marker)]), 1)
        self.assertFalse(marker.exists())
        log = "\n".join(lines)
        self.assertIn("sudo 需要输入密码", log)
        self.assertIn("NOPASSWD", log)
        self.assertIn("sudo touch", log)  # 给出可以直接照抄的命令

    def test_interactive_stream_still_lets_sudo_ask_for_a_password(self):
        """CLI 的 --json 仍有终端：不要用免密预检拦住它，sudo 自己弹提示。"""
        self.settings.root = Path("/")
        lines = []
        installer = Installer(self.settings, Mock(), lines.append, stream=lines.append)
        installer.sudo_ready = lambda: False
        process = Mock()
        process.stdout = io.BytesIO("sudo: 需要密码\n".encode())
        process.wait.return_value = 1
        with patch("dick.install.sys.stdin", Mock(isatty=lambda: True)), \
                patch("dick.install.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(installer.execute(["sudo", "true"]), 1)
        self.assertEqual(popen.call_args.args[0], ["sudo", "true"])
        self.assertIn("sudo: 需要密码", lines)
        self.assertNotIn("NOPASSWD", "\n".join(lines))

    def test_sudo_ready_is_asked_once(self):
        installer = self.installer(Mock())
        with patch("dick.install.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(installer.sudo_ready())
            self.assertTrue(installer.sudo_ready())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "true"])

    def test_client_disconnects_do_not_print_tracebacks(self):
        handler = Handler.__new__(Handler)
        handler.close_connection = False
        with patch("dick.web.BaseHTTPRequestHandler.handle_one_request",
                   side_effect=ConnectionResetError(104, "Connection reset by peer")):
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)

    def test_native_failure_falls_back_to_linyaps(self):
        index = Mock()
        index.search.side_effect = [([Package("firefox", "pacman", repository="extra")], []),
                                    ([Package("org.mozilla.firefox", "linyaps", repository="stable")], [])]
        installer = self.installer(index)
        with patch.object(self.settings, "available", return_value=True), \
                patch("dick.install.subprocess.run", side_effect=[subprocess.CompletedProcess([], 1),
                                                                  subprocess.CompletedProcess([], 0)]) as run:
            result = installer.install("firefox", ["pacman", "linyaps"])
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "linyaps")
        self.assertEqual(run.call_args_list[1].args[0], ["ll-cli", "install", "org.mozilla.firefox"])


class CLITests(FixtureTest):
    def run_cli(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def common(self, *extra):
        return ["--root", str(self.root), "--cache-dir", str(self.root / "cli-cache"), *extra]

    def test_arch_update_search_and_dry_run_install_end_to_end(self):
        mirror = self.write("mirror/extra.db", pacman_database())
        self.write("etc/pacman.conf", f"[extra]\nServer = {mirror.parent.as_uri()}\n")
        with patch.object(Settings, "available", side_effect=lambda source: source == "pacman"), \
                patch.object(Installer, "privileged", lambda self, command: command):
            code, output, _ = self.run_cli(self.common("--source", "pacman", "--json", "update"))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["refreshed"][0]["repository"], "extra")
            for action in (["search", "firefox"], ["search", "firefox", "--exact"]):
                with self.subTest(action=action):
                    code, output, _ = self.run_cli(self.common("--source", "pacman", "--json", *action))
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(output)["packages"][0]["name"], "firefox")
            code, output, _ = self.run_cli(self.common("--source", "pacman", "--json", "install", "firefox", "--dry-run"))
            self.assertEqual(code, 0)
            attempt = json.loads(output)["results"][0]["attempts"][0]
            self.assertEqual(attempt["command"], ["pacman", "-S", "--", "extra/firefox"])
            self.assertEqual(attempt["returncode"], 0)

    def test_search_without_match_on_missing_source_fails(self):
        mirror = self.write("mirror/extra.db", pacman_database())
        self.write("etc/pacman.conf", f"[extra]\nServer = {mirror.parent.as_uri()}\n")
        with patch.object(Settings, "available", side_effect=lambda source: source == "pacman"):
            self.run_cli(self.common("--source", "pacman", "--json", "update"))
            code, output, _ = self.run_cli(self.common("--source", "pacman", "--json", "search", "nosuchpackage"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["packages"], [])

    def test_linyaps_search_and_dry_run_install_end_to_end(self):
        search = json.dumps({"stable": [{"id": "org.deepin.calculator", "name": "deepin-calculator",
                                         "version": "6.5.26.1", "description": "Calculator for UOS"}]})
        repositories = json.dumps({"defaultRepo": "stable", "repos": [{"name": "stable", "url": "https://repo"}]})
        common = self.common("--source", "linyaps", "--json")
        with patch.object(Settings, "available", side_effect=lambda source: source == "linyaps"), \
                patch("dick.discovery.native_output", return_value=repositories), \
                patch("dick.index.native_output", return_value=search):
            code, output, _ = self.run_cli([*common, "search", "calculator"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["packages"][0]["name"], "org.deepin.calculator")
            code, output, _ = self.run_cli([*common, "install", "org.deepin.calculator", "--dry-run"])
        self.assertEqual(code, 0)
        attempt = json.loads(output)["results"][0]["attempts"][0]
        self.assertEqual(attempt["command"], ["sudo", "ll-cli", "install", "org.deepin.calculator"])
        self.assertEqual(attempt["returncode"], 0)

    def test_source_enable_disable_list_and_scan(self):
        config = self.root / "conf" / "config.toml"
        code, output, _ = self.run_cli(self.common("--config", str(config), "--json", "source", "disable", "flatpak"))
        self.assertEqual(code, 0)
        self.assertNotIn("flatpak", json.loads(output)["enabled"])
        remaining = [source for source in SOURCES if source != "flatpak"]
        self.assertIn("enabled = [" + ", ".join(f'"{source}"' for source in remaining) + "]",
                      config.read_text(encoding="utf-8"))
        with patch("dick.discovery.native_output") as native:
            code, output, _ = self.run_cli(self.common("--config", str(config), "--json", "source", "list"))
        self.assertEqual(code, 0)
        native.assert_not_called()
        states = {entry["source"]: entry["enabled"] for entry in json.loads(output)["sources"]}
        self.assertFalse(states["flatpak"])
        self.assertTrue(states["pacman"])
        code, _, errors = self.run_cli(self.common("--config", str(config), "--source", "flatpak", "list"))
        self.assertEqual(code, 1)
        self.assertIn("已禁用", errors)
        with patch.object(Settings, "available", side_effect=lambda source: source == "flatpak"), \
                patch("dick.discovery.native_output",
                      return_value="flathub\thttps://dl.flathub.org/repo/\n"):
            code, output, _ = self.run_cli(self.common("--config", str(config), "--source", "flatpak",
                                                       "--json", "source", "scan"))
        self.assertEqual(code, 0)
        entry = next(entry for entry in json.loads(output)["sources"] if entry["source"] == "flatpak")
        self.assertEqual([repository["repository"] for repository in entry["repositories"]], ["flathub"])
        code, output, _ = self.run_cli(self.common("--config", str(config), "--json", "source", "enable", "flatpak"))
        self.assertEqual(code, 0)
        self.assertIn("flatpak", json.loads(output)["enabled"])

    def test_list_reads_installed_packages_per_source(self):
        installed = {
            "pacman": [LocalPackage("pacman", "firefox", "130.0-1"), LocalPackage("pacman", "firefox-esr", "115")],
            "linyaps": [LocalPackage("linyaps", "org.mozilla.firefox", "130", "Firefox")],
        }
        common = self.common("--json")
        with patch.object(Settings, "available", return_value=True), \
                patch("dick.discovery.native_output", return_value="flathub\thttps://dl.flathub.org/repo/\n"), \
                patch("dick.cli.read_installed", side_effect=lambda settings, source: installed.get(source, [])):
            code, output, _ = self.run_cli([*common, "list", "--source", "pacman"])
            self.assertEqual(code, 0)
            self.assertEqual([package["name"] for package in json.loads(output)["packages"]],
                             ["firefox", "firefox-esr"])
            code, output, errors = self.run_cli([*common, "list", "firefox", "--limit", "1",
                                                 "--source", "pacman", "--source", "linyaps"])
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["total"], 3)
            self.assertEqual([package["name"] for package in payload["packages"]],
                             ["firefox", "org.mozilla.firefox"])
            code, output, errors = self.run_cli(["--root", str(self.root),
                                                 "--cache-dir", str(self.root / "cli-cache"),
                                                 "--source", "pacman", "--source", "linyaps",
                                                 "list", "firefox", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["[pacman] firefox 130.0-1", "[linyaps] org.mozilla.firefox 130"])
        self.assertIn("pacman 共 2 个", errors)
        self.assertIn("--limit", errors)

    def test_remove_requires_confirmation_and_scans_installed_packages(self):
        installed = {"pacman": [LocalPackage("pacman", "firefox", "130.0-1")]}
        sandbox = ["--json", "--source", "pacman"]
        with patch.object(Settings, "available", side_effect=lambda source: source == "pacman"), \
                patch("dick.cli.read_installed", side_effect=lambda settings, source: installed.get(source, [])), \
                patch.object(Installer, "privileged", lambda self, command: ["sudo", *command]):
            code, _, errors = self.run_cli(["--cache-dir", str(self.root / "cli-cache"), *sandbox,
                                            "remove", "firefox"])
            self.assertEqual(code, 1)
            self.assertIn("--yes", errors)
            code, output, _ = self.run_cli(self.common(*sandbox, "remove", "firefox", "--yes", "--dry-run"))
            self.assertEqual(code, 0)
            result = json.loads(output)["results"][0]
            self.assertEqual(result["command"], ["sudo", "pacman", "-R", "--noconfirm", "--", "firefox"])
            self.assertTrue(result["success"])
            code, output, _ = self.run_cli(self.common(*sandbox, "remove", "uninstalled", "--dry-run"))
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output)["results"][0]["success"])

    def test_remove_ambiguous_candidates_need_explicit_choice(self):
        installed = {"pacman": [LocalPackage("pacman", "firefox", "130.0-1"),
                                LocalPackage("pacman", "firefox-esr", "115")]}
        sandbox = ["--json", "--source", "pacman"]
        with patch.object(Settings, "available", side_effect=lambda source: source == "pacman"), \
                patch("dick.cli.read_installed", side_effect=lambda settings, source: installed.get(source, [])):
            code, _, errors = self.run_cli(["--cache-dir", str(self.root / "cli-cache"), *sandbox,
                                            "remove", "firefox", "--yes"])
            self.assertEqual(code, 1)
            self.assertIn("匹配到多个已安装的包", errors)
            code, output, _ = self.run_cli(self.common(*sandbox, "remove", "firefox", "--yes", "--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual([result["package"] for result in json.loads(output)["results"]],
                         ["firefox", "firefox-esr"])

    def test_choose_candidates_prompts_and_validates_input(self):
        candidates = [LocalPackage("pacman", "firefox"), LocalPackage("flatpak", "org.mozilla.firefox")]
        with patch("dick.cli.sys.stdin", Mock(isatty=lambda: True)), \
                patch("builtins.input", side_effect=["x", "9", "2,1"]):
            with contextlib.redirect_stdout(io.StringIO()):
                chosen = choose_candidates("firefox", candidates, Mock(json=False))
        self.assertEqual([package.source for package in chosen], ["flatpak", "pacman"])
        with patch("dick.cli.sys.stdin", Mock(isatty=lambda: True)), patch("builtins.input", return_value=""):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(choose_candidates("firefox", candidates, Mock(json=False)), [])
        with self.assertRaisesRegex(DickError, "匹配到多个"):
            choose_candidates("firefox", candidates, Mock(json=True))

    def test_upgrade_plan_uses_native_manager(self):
        with patch.object(Settings, "available", side_effect=lambda source: source == "pacman"), \
                patch.object(Installer, "privileged", lambda self, command: ["sudo", *command]):
            code, output, _ = self.run_cli(self.common("--json", "upgrade", "--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["command"], ["sudo", "pacman", "-Syu"])
        with patch.object(Settings, "available", side_effect=lambda source: source == "flatpak"):
            code, _, errors = self.run_cli(self.common("upgrade", "--dry-run"))
        self.assertEqual(code, 1)
        self.assertIn("upgrade 只作用于", errors)

    def test_fixture_root_blocks_real_system_mutation(self):
        with contextlib.redirect_stderr(io.StringIO()) as output, patch("dick.install.subprocess.run") as run:
            self.assertEqual(main(["--root", str(self.root), "install", "firefox"]), 1)
        self.assertIn("--root", output.getvalue())
        run.assert_not_called()


class SelfManageTests(FixtureTest):
    """dick updateme / dick removeme：只碰 install.sh 装出来的目录结构。"""

    def run_cli(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def script_installation(self, prefix=None):
        """造一个 install.sh 的目录结构：share/dick/{venv,src} + bin/dick 软链。"""
        prefix = Path(prefix or self.root / "prefix")
        for part in ("share/dick/venv/bin", "share/dick/src/.git", "bin"):
            (prefix / part).mkdir(parents=True, exist_ok=True)
        (prefix / "share/dick/src/pyproject.toml").write_text("[project]\nname = \"dick\"\n")
        for name in ("pip", "python", "dick"):
            (prefix / "share/dick/venv/bin" / name).write_text("#!/bin/sh\n")
        (prefix / "bin/dick").symlink_to(prefix / "share/dick/venv/bin/dick")
        return prefix

    def args_for(self, *extra):
        args, remaining = options([*extra])
        self.assertEqual(normalize(remaining).name, "updateme" if "updateme" in remaining else "removeme")
        return args

    def test_script_layout_is_detected_and_update_plan_is_complete(self):
        prefix = self.script_installation()
        installation = selfmanage.detect(str(prefix))
        self.assertEqual(installation.kind, "script")
        self.assertEqual(installation.share, prefix / "share/dick")
        commands = selfmanage.update_commands(installation, "main")
        self.assertEqual([Path(command[0]).name for command in commands], ["git", "git", "pip"])
        self.assertEqual(commands[0][-2:], ["origin", "main"])
        self.assertEqual(commands[2][-2:], ["--upgrade", str(prefix / "share/dick/src")])
        # --ref 决定拉哪个分支
        self.assertEqual(selfmanage.update_commands(installation, "v0.2")[0][-1], "v0.2")

    def test_detect_uses_the_configured_prefix_then_reports_unknown(self):
        prefix = self.script_installation()
        with patch.dict(os.environ, {"DICK_PREFIX": str(prefix)}):
            self.assertEqual(selfmanage.detect().kind, "script")
        with self.assertRaisesRegex(DickError, "没找到 share/dick"):
            selfmanage.detect(str(self.root / "nothing-here"))

    def test_updateme_reports_the_version_change_and_dry_run(self):
        prefix = self.script_installation()

        versions = iter(["0.1.0\n", "0.2.0\n"])

        def newer(command, cwd=None):
            if Path(command[0]).name == "python":
                return 0, next(versions)
            return 0, "Already up to date."

        args = self.args_for("--prefix", str(prefix), "updateme")
        with patch.object(selfmanage, "run_command", side_effect=newer):
            payload = selfmanage.run_update(self.settings, args)
        self.assertTrue(payload["success"])
        self.assertEqual((payload["before"], payload["after"]), ("0.1.0", "0.2.0"))
        self.assertIn("0.1.0 → 0.2.0", payload["message"])
        self.assertIn("Already up to date.", payload["lines"])

        # 演练：一条命令都不执行，版本也不去读
        dry = self.args_for("--prefix", str(prefix), "updateme", "--dry-run")
        with patch.object(selfmanage, "run_command", side_effect=AssertionError("演练不应执行命令")):
            payload = selfmanage.run_update(self.settings, dry)
        self.assertTrue(payload["dry_run"] and payload["success"])
        self.assertEqual(payload["lines"], [])
        self.assertEqual(payload["after"], payload["before"])
        self.assertIn("演练", payload["message"])

    def test_updateme_keeps_the_reason_when_a_command_fails(self):
        prefix = self.script_installation()

        def broken(command, cwd=None):
            if Path(command[0]).name == "python":
                return 0, "0.1.0\n"
            return 128, "致命错误：不是 Git 仓库"

        args = self.args_for("--prefix", str(prefix), "updateme")
        with patch.object(selfmanage, "run_command", side_effect=broken):
            payload = selfmanage.run_update(self.settings, args)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["returncode"], 128)
        self.assertIn("退出码 128", payload["error"])
        self.assertIn("不是 Git 仓库", payload["lines"][0])
        self.assertIn("更新失败", payload["message"])

    def test_updateme_refuses_system_and_unknown_installations(self):
        for kind, hint, pattern in (("system", "请用装它的包管理器", "包管理器"),
                                    ("unknown", "没找到 DICK 的安装位置", "没找到")):
            with self.subTest(kind=kind), \
                    patch.object(selfmanage, "detect", return_value=selfmanage.Installation(kind, hint=hint)), \
                    self.assertRaisesRegex(DickError, pattern):
                selfmanage.run_update(self.settings, self.args_for("updateme"))

    def test_update_commands_pull_the_checkout_and_tolerate_a_tarball_install(self):
        checkout = selfmanage.Installation("checkout", source=self.root)
        self.assertEqual(selfmanage.update_commands(checkout, "main"),
                         [["git", "-C", str(self.root), "pull", "--ff-only"]])
        # 没有 src（当初用 tarball 装的）：让 pip 直接取仓库
        prefix = self.script_installation()
        installation = selfmanage.detect(str(prefix))
        installation.source = None
        self.assertEqual(selfmanage.update_commands(installation, "main")[0][-1],
                         f"git+{selfmanage.REPO_URL}@main")

    def test_removeme_deletes_the_installation_but_keeps_the_config(self):
        prefix = self.script_installation()
        config = self.write("self/etc/dick.toml", "")
        code, output, errors = self.run_cli(["--prefix", str(prefix), "--config", str(config),
                                             "--cache-dir", str(self.root / "self-cache"),
                                             "removeme", "--yes"])
        self.assertEqual(code, 0)
        self.assertFalse((prefix / "share/dick").exists())
        self.assertFalse((prefix / "bin/dick").is_symlink())
        self.assertTrue(config.exists())
        self.assertIn("保留配置", errors)

    def test_removeme_purge_also_removes_config_and_cache(self):
        prefix = self.script_installation()
        config = self.write("purge/etc/dick.toml", "")
        cache = self.write("purge/cache/keep.txt", "")
        code, output, _ = self.run_cli(["--prefix", str(prefix), "--config", str(config),
                                        "--cache-dir", str(cache.parent),
                                        "--json", "removeme", "--yes", "--purge"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["kind"], "script")
        self.assertIn(str(config.parent), payload["removed"])
        self.assertFalse(config.parent.exists())
        self.assertFalse(cache.parent.exists())

    def test_removeme_dry_run_lists_targets_without_deleting(self):
        prefix = self.script_installation()
        code, output, errors = self.run_cli(["--prefix", str(prefix), "--cache-dir",
                                             str(self.root / "dry-cache"), "--json",
                                             "removeme", "--dry-run"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["removed"], [str(prefix / "share/dick"), str(prefix / "bin/dick")])
        self.assertTrue((prefix / "share/dick").is_dir())
        self.assertIn("演练", payload["message"])
        self.assertEqual(errors, "")

    def test_removeme_refuses_a_source_checkout_and_missing_confirmation(self):
        with patch.object(selfmanage, "detect",
                          return_value=selfmanage.Installation("checkout", source=self.root)), \
                self.assertRaisesRegex(DickError, "源码工作区"):
            selfmanage.run_remove(self.settings, self.args_for("removeme", "--yes"))
        prefix = self.script_installation()
        args = self.args_for("--prefix", str(prefix), "removeme")
        with patch.object(sys, "stdin", io.StringIO("")), \
                self.assertRaisesRegex(DickError, "需要 --yes"):
            selfmanage.run_remove(self.settings, args)
        self.assertTrue((prefix / "share/dick").is_dir())

    def test_removeme_writes_nothing_outside_its_prefix(self):
        """回归：removeme 只删安装目录与那个软链，绝不越界删别的路径。"""
        prefix = self.script_installation()
        outsider = self.write("bystander/important.txt", "keep me")
        code, _, _ = self.run_cli(["--prefix", str(prefix), "--config", str(self.config),
                                   "--cache-dir", str(self.root / "outside-cache"),
                                   "--json", "removeme", "--yes"])
        self.assertEqual(code, 0)
        self.assertTrue(outsider.exists())
        self.assertTrue(self.config.exists())


class AITests(FixtureTest):
    """AI 翻译：配置解析、响应兼容与磁盘缓存（联网部分全部打桩）。"""

    AI = ('[ai]\nenabled = true\nbase_url = "https://api.example.com/v1"\n'
          'model = "test-model"\napi_key = "sk-test"\ntarget = "中文"\n')

    def settings_for(self, text):
        path = self.write("etc/dick.toml", text)
        return Settings(config_path=path, root=self.root, cache_dir=self.root / "cache")

    def test_unconfigured_translator_reports_and_refuses(self):
        translator = Translator(self.settings_for('[ai]\nmodel = "m"\n'))
        self.assertFalse(translator.configured)
        self.assertFalse(translator.describe()["configured"])
        with self.assertRaisesRegex(DickError, "未配置 AI"):
            translator.translate(["hello"])
        with self.assertRaisesRegex(DickError, "未配置 AI"):
            translator.test()

    def test_openai_response_is_parsed_and_cached(self):
        translator = Translator(self.settings_for(self.AI))
        payload = {"choices": [{"message": {"content": '["你好", "世界"]'}}]}
        with patch.object(Translator, "_post", return_value=payload) as post:
            self.assertEqual(translator.translate(["hello", "world"]), ["你好", "世界"])
            url, body, headers = post.call_args.args
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertEqual(headers["Authorization"], "Bearer sk-test")
            self.assertEqual(len(body["messages"]), 2)
            self.assertEqual(translator.translate(["hello", "world"]), ["你好", "世界"])
            self.assertEqual(post.call_count, 1)  # 第二次命中磁盘缓存，不再请求接口
        self.assertEqual(translator.describe()["timeout"], 60)
        self.assertTrue(translator.describe()["has_key"])

    def test_anthropic_response_in_code_fence(self):
        text = self.AI.replace("https://api.example.com/v1", "https://api.anthropic.com/v1")
        translator = Translator(self.settings_for(text))
        payload = {"content": [{"text": "```json\n[\"你好\"]\n```"}]}
        with patch.object(Translator, "_post", return_value=payload) as post:
            self.assertEqual(translator.translate(["hello"]), ["你好"])
            url, body, headers = post.call_args.args
        self.assertTrue(url.endswith("/messages"))
        self.assertIn("x-api-key", headers)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], "test-model")

    def test_mismatched_length_is_rejected(self):
        translator = Translator(self.settings_for(self.AI))
        payload = {"choices": [{"message": {"content": '["只有一条"]'}}]}
        with patch.object(Translator, "_post", return_value=payload):
            with self.assertRaisesRegex(DickError, "等长"):
                translator.translate(["a", "b"])

    def test_ai_settings_are_validated(self):
        with self.assertRaisesRegex(DickError, "ai.timeout 必须是数字"):
            self.settings_for('[ai]\ntimeout = "slow"\n')
        with self.assertRaisesRegex(DickError, "ai.timeout 必须大于零"):
            self.settings_for("[ai]\ntimeout = 0\n")
        with self.assertRaisesRegex(DickError, "ai.base_url"):
            self.settings_for('[ai]\nbase_url = "ftp://x"\n')
        with self.assertRaisesRegex(DickError, "ai.enabled"):
            self.settings_for('[ai]\nenabled = "yes"\n')

    def test_set_ai_preserves_comments_and_round_trips(self):
        path = self.write("etc/dick.toml", "# 我的注释\n\n[sources]\nenabled = [\"pacman\"]\n")
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache")
        state = settings.set_ai({"enabled": True, "api_key": "sk-1", "model": "m",
                                 "target": "日语", "timeout": 45})
        self.assertTrue(state["configured"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 我的注释", text)
        self.assertIn("timeout = 45", text)
        again = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache")
        self.assertEqual(again.ai["model"], "m")
        self.assertEqual(again.ai["target"], "日语")
        self.assertEqual(again.ai["timeout"], 45)
        self.assertEqual(again.enabled_sources, ("pacman",))

    def test_web_settings_defaults_and_validation(self):
        settings = self.settings_for("[web]\n")
        self.assertEqual((settings.web_host, settings.web_port), ("127.0.0.1", 3907))
        with self.assertRaisesRegex(DickError, "web.port"):
            self.settings_for("[web]\nport = 700000\n")
        with self.assertRaisesRegex(DickError, "web.host"):
            self.settings_for("[web]\nhost = 1\n")


class WebCommandTests(FixtureTest):
    def test_web_command_and_options(self):
        args, remaining = options(["--host", "0.0.0.0", "--port", "3908", "--open", "web"])
        self.assertEqual((args.host, args.port, args.open_browser), ("0.0.0.0", 3908, True))
        self.assertEqual(normalize(remaining), Action(name="web"))
        with self.assertRaisesRegex(DickError, "--port 必须在 1-65535 之间"):
            options(["--port", "70000", "web"])

    def test_web_command_rejects_positional_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["web", "extra"]), 1)
        self.assertIn("web 不接受参数", output.getvalue())


class WebAppTests(FixtureTest):
    """WebApp 路由的纯函数级测试：不联网、不读取真实系统。"""

    REPOSITORIES = [Repository("pacman", "core", ("https://mirror.example/core.db",))]

    def app(self):
        path = self.write("etc/dick.toml", "")
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache", create=True)
        settings.architecture = "x86_64"
        return WebApp(settings, offline=True)

    def wait(self, app, job_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = app.job_by_id(job_id).snapshot(0)
            if snapshot["status"] != "running":
                return snapshot
            time.sleep(0.01)
        self.fail(f"任务 {job_id} 超时未结束")

    def test_status_reports_every_source(self):
        app = self.app()
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])):
            payload = app.status({}, {})
        self.assertEqual([entry["source"] for entry in payload["sources"]], list(SOURCES))
        self.assertEqual(payload["sources"][0]["repositories"], 1)
        self.assertEqual(payload["family"], "arch")
        self.assertFalse(payload["ai"]["configured"])

    def test_sources_scan_flag_controls_native_probing(self):
        app = self.app()
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, ["警告"])) as scan:
            payload = app.sources({"scan": "1"}, {})
        self.assertTrue(payload["scanned"])
        self.assertEqual(payload["errors"], ["警告"])
        self.assertEqual(scan.call_args.kwargs["native"], True)
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])) as cached:
            app.sources({}, {})
        self.assertEqual(cached.call_args.kwargs["native"], False)

    def test_enable_and_disable_sources_write_config(self):
        app = self.app()
        payload = app.disable_sources({}, {"sources": ["flatpak", "snap"]})
        self.assertEqual(payload["enabled"],
                         [source for source in SOURCES if source not in {"flatpak", "snap"}])
        again = Settings(config_path=app.settings.config_path, root=self.root, cache_dir=self.root / "cache")
        self.assertFalse(again.enabled("flatpak"))
        self.assertIn("flatpak", app.enable_sources({}, {"sources": ["flatpak"]})["enabled"])
        with self.assertRaisesRegex(DickError, "未知来源"):
            app.enable_sources({}, {"sources": ["pip"]})
        with self.assertRaisesRegex(DickError, "需要 sources 列表"):
            app.disable_sources({}, {"sources": []})

    def test_installed_uses_every_enabled_source(self):
        app = self.app()
        package = LocalPackage("pacman", "firefox", "130.0-1", "Web browser")

        def read(settings, source):
            return [package] if source == "pacman" else []

        with patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", side_effect=read):
            payload = app.installed({"q": "fire"}, {})
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["packages"][0]["name"], "firefox")
        self.assertTrue(payload["packages"][0]["icon"].startswith("/api/icon?"))
        self.assertEqual(payload["sources"], [source for source in SOURCES])

    def test_candidates_rank_matches_and_require_target(self):
        app = self.app()
        packages = [LocalPackage("pacman", "firefox", "130.0-1"),
                    LocalPackage("linyaps", "cn.wps.wps-office", "12.1.2")]

        def read(settings, source):
            return [package for package in packages if package.source == source]

        with patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", side_effect=read):
            payload = app.candidates({"target": "wps"}, {})
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["candidates"][0]["name"], "cn.wps.wps-office")
            self.assertEqual(payload["candidates"][0]["rank"], 3)  # 子串匹配，不是精确同名
            exact = app.candidates({"target": "cn.wps.wps-office"}, {})
        self.assertEqual(exact["candidates"][0]["rank"], 0)
        with self.assertRaisesRegex(DickError, "缺少 target"):
            app.candidates({}, {})

    def test_package_detail_builds_commands(self):
        app = self.app()
        with patch("dick.web.read_installed", return_value=[]), \
                patch.object(Settings, "available", return_value=True):
            detail = app.package({"source": "pacman", "name": "firefox",
                                  "version": "130.0-1", "repository": "core"}, {})
        self.assertEqual(detail["install_command"], ["sudo", "pacman", "-S", "--", "core/firefox"])
        self.assertFalse(detail["installed"])
        with self.assertRaisesRegex(DickError, "合法的 source"):
            app.package({"source": "pip", "name": "firefox"}, {})
        with self.assertRaisesRegex(DickError, "需要 name"):
            app.package({"source": "pacman"}, {})

    def test_action_requires_preview_or_confirmation(self):
        app = self.app()
        with self.assertRaisesRegex(DickError, "演练"):
            app.action({}, {"action": "install", "targets": ["firefox"]})
        with self.assertRaisesRegex(DickError, "action 必须是"):
            app.action({}, {"action": "purge"})
        with self.assertRaisesRegex(DickError, "install 需要 targets"):
            app.action({}, {"action": "install", "dry_run": True})
        with self.assertRaisesRegex(DickError, "targets 必须是包名列表"):
            app.action({}, {"action": "install", "dry_run": True, "targets": ["-rf"]})
        with self.assertRaisesRegex(DickError, "packages 必须是"):
            app.action({}, {"action": "remove", "dry_run": True, "packages": [{"name": ""}]})
        with self.assertRaisesRegex(DickError, "upgrade 不接受 targets"):
            app.action({}, {"action": "upgrade", "dry_run": True, "targets": ["firefox"]})
        with self.assertRaisesRegex(DickError, "sources 必须是来源列表"):
            app.action({}, {"action": "upgrade", "dry_run": True, "sources": ["pip"]})
        with self.assertRaisesRegex(DickError, "真实根目录"):
            app.action({}, {"action": "upgrade", "confirm": True, "sources": ["pacman"]})

    def test_remove_job_plans_explicit_choice(self):
        app = self.app()
        package = LocalPackage("linyaps", "cn.wps.wps-office", "12.1.2")

        def read(settings, source):
            return [package] if source == "linyaps" else []

        with patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", side_effect=read):
            payload = app.action({}, {"action": "remove", "dry_run": True,
                                      "packages": [{"source": "linyaps", "name": "cn.wps.wps-office"}]})
            snapshot = self.wait(app, payload["job"]["id"])
        self.assertEqual(snapshot["status"], "done")
        self.assertIn("ll-cli uninstall cn.wps.wps-office", "\n".join(snapshot["lines"]))
        self.assertEqual(payload["request"]["sources"], [source for source in SOURCES])

    def test_remove_job_reports_why_a_running_app_cannot_be_uninstalled(self):
        """网页里的卸载失败以前只说「退出码 255」；现在要把 ll-cli 的原话和 ll-cli kill 提示带出来。"""
        app = self.app()
        app.settings.root = Path("/")  # action 只允许在真实根目录下做系统变更
        package = LocalPackage("linyaps", "com.qq.music", "1.1.8.3")

        def read(settings, source):
            return [package] if source == "linyaps" else []

        def factory(settings, index, report, dry_run=False, yes=False, stream=None, password=None):
            built = Installer(settings, index, report, dry_run, yes, stream=stream, password=password)
            built.last_output = [
                "执行：sudo ll-cli uninstall com.qq.music",
                "The application is currently running and cannot be uninstalled. "
                "Please turn off the application and try again."]
            built.execute = lambda command: 255
            return built

        with patch("dick.web.Installer", side_effect=factory), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", side_effect=read):
            payload = app.action({}, {"action": "remove", "confirm": True, "yes": True,
                                      "packages": [{"source": "linyaps", "name": "com.qq.music"}]})
            snapshot = self.wait(app, payload["job"]["id"])
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["result"]["results"][0]["error"],
                         "The application is currently running and cannot be uninstalled. "
                         "Please turn off the application and try again.")
        self.assertIn("ll-cli kill com.qq.music", "\n".join(snapshot["lines"]))

    def test_upgrade_job_uses_native_manager(self):
        app = self.app()
        with patch.object(Settings, "available", return_value=True):
            payload = app.action({}, {"action": "upgrade", "dry_run": True, "sources": ["pacman"]})
            snapshot = self.wait(app, payload["job"]["id"])
        self.assertEqual(snapshot["status"], "done")
        self.assertIn("pacman -Syu", "\n".join(snapshot["lines"]))

    def test_unknown_job_is_reported(self):
        with self.assertRaisesRegex(DickError, "未知任务"):
            self.app().job_by_id("deadbeef")

    def test_ai_save_round_trip_never_leaks_key(self):
        app = self.app()
        state = app.ai_save({}, {"enabled": True, "base_url": "https://api.example.com/v1",
                                 "model": "m", "api_key": "sk-1", "target": "日语", "timeout": 20})
        self.assertTrue(state["configured"])
        self.assertTrue(state["has_key"])
        self.assertNotIn("sk-1", json.dumps(state))
        again = Settings(config_path=app.settings.config_path, root=self.root, cache_dir=self.root / "cache")
        self.assertEqual(again.ai["timeout"], 20)
        self.assertEqual(again.ai["api_key"], "sk-1")
        with self.assertRaisesRegex(DickError, "未知设置项"):
            app.ai_save({}, {"nope": 1})
        with self.assertRaisesRegex(DickError, "enabled 必须是布尔值"):
            app.ai_save({}, {"enabled": "yes"})
        with self.assertRaisesRegex(DickError, "timeout 必须大于零"):
            app.ai_save({}, {"timeout": -1})
        with self.assertRaisesRegex(DickError, "没有要保存的设置"):
            app.ai_save({}, {})

    def test_translate_and_ai_test_require_configuration(self):
        app = self.app()
        with self.assertRaisesRegex(DickError, "未配置 AI"):
            app.translate({}, {"texts": ["hello"]})
        with self.assertRaisesRegex(DickError, "translate 需要 texts 列表"):
            app.translate({}, {})
        with self.assertRaisesRegex(DickError, "text 必须是字符串"):
            app.ai_test({}, {"text": 5})

    def test_icon_falls_back_to_local_avatar(self):
        app = self.app()
        content, content_type = app.icon({"source": "pacman", "name": "firefox"}, {})
        self.assertEqual(content_type, "image/svg+xml")
        self.assertIn(b"<svg", content)
        with self.assertRaisesRegex(DickError, "icon 需要 name"):
            app.icon({"source": "pacman"}, {})


class CatalogTests(unittest.TestCase):
    """精选目录是纯静态数据，这里只校验它自身自洽。"""

    def test_categories_are_unique_and_complete(self):
        ids = [category.id for category in catalog.CATEGORIES]
        self.assertEqual(len(ids), len(set(ids)), "分类 id 不能重复")
        for category in catalog.CATEGORIES:
            with self.subTest(category=category.id):
                self.assertTrue(category.title and category.subtitle and category.icon)

    def test_every_app_points_at_a_real_category(self):
        keys = [app.key for app in catalog.APPS]
        self.assertEqual(len(keys), len(set(keys)), "应用 key 不能重复")
        self.assertGreaterEqual(len(catalog.APPS), 50)
        for app in catalog.APPS:
            with self.subTest(app=app.key):
                self.assertIn(app.category, catalog.CATEGORIES_BY_ID)
                self.assertTrue(app.name and app.tagline)
                self.assertTrue(app.names())
                for source in (*app.sources, *app.on_demand):
                    self.assertIn(source, SOURCES)
                self.assertTrue(set(app.on_demand) <= set(SOURCES) | {"aur", "snap", "linyaps"})

    def test_in_category_and_title_lookup(self):
        featured = catalog.in_category("featured")
        self.assertTrue(featured)
        self.assertTrue(all(app.featured for app in featured))
        self.assertEqual({app.key for app in catalog.featured_apps()},
                         {app.key for app in featured})
        dev = catalog.in_category("dev")
        self.assertTrue(dev)
        self.assertTrue(all(app.category == "dev" for app in dev))
        self.assertEqual(catalog.in_category("nope"), [])
        self.assertEqual(catalog.category_title("dev"), "开发工具")
        self.assertEqual(catalog.category_title("nope"), "nope")

    def test_names_falls_back_to_the_key(self):
        plain = catalog.App("htop", "htop", "交互式进程查看器", "system")
        self.assertEqual(plain.names(), ("htop",))
        self.assertEqual(catalog.APPS_BY_KEY["code"].names()[0], "code")


class FeaturedTests(FixtureTest):
    """精选接口：把静态目录解析到受控的假索引上，不联网、不读真实系统。"""

    REPOSITORIES = [Repository("pacman", "core", ("https://mirror.example/core.db",))]
    INDEX = [
        Package("visual-studio-code", "pacman", "Code editor", "1.96.0-1", "extra", "x86_64"),
        Package("firefox", "pacman", "Web browser", "133.0-1", "extra", "x86_64"),
        Package("htop", "pacman", "Interactive process viewer", "3.3.0-1", "extra", "x86_64"),
        Package("cn.wps.wps-office", "linyaps", "WPS Office", "12.1.2", "", ""),
    ]

    def app(self, config=""):
        path = self.write("etc/dick.toml", config)
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache", create=True)
        settings.architecture = "x86_64"
        return WebApp(settings, offline=True)

    def fake_search(self, calls, failures=()):
        def search(query, sources, exact=False):
            calls.append((query, tuple(sources), exact))
            if query == "":
                return [package for package in self.INDEX if package.source in sources], list(failures)
            if exact:
                return [package for package in self.INDEX if package.name.casefold() == query.casefold()
                        and package.source in sources], []
            return [], []

        return search

    def records(self, payload):
        return {app["key"]: app for section in payload["sections"] for app in section["apps"]}

    def test_resolves_local_apps_and_reports_structure(self):
        app = self.app()
        calls = []
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])), \
                patch.object(Index, "search", side_effect=self.fake_search(calls)), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=[]):
            payload = app.featured({"sources": "pacman"}, {})
        self.assertEqual(payload["sources"], ["pacman"])
        self.assertEqual(payload["total"], len(catalog.APPS))
        self.assertEqual(payload["errors"], [])
        self.assertEqual([section["id"] for section in payload["sections"]],
                         [category.id for category in catalog.CATEGORIES])
        self.assertEqual([entry["count"] for entry in payload["categories"]],
                         [len(catalog.in_category(category.id)) for category in catalog.CATEGORIES])
        records = self.records(payload)
        code = records["code"]
        self.assertTrue(code["resolved"])
        self.assertEqual(code["package"], "visual-studio-code")
        self.assertEqual(code["repository"], "extra")
        self.assertEqual(code["category_title"], "开发工具")
        self.assertIn("/api/icon?source=pacman&name=visual-studio-code", code["icon"])
        # 目录里没被索引命中的应用退化成字母头像 + 去搜索
        gimp = records["gimp"]
        self.assertFalse(gimp["resolved"])
        self.assertIsNone(gimp["package"])
        self.assertIn("/api/icon?source=other&name=", gimp["icon"])
        self.assertEqual(payload["resolved"], 3)
        self.assertTrue(payload["hero"]["resolved"])
        self.assertIn(payload["hero"]["key"], {item.key for item in catalog.featured_apps()})
        self.assertEqual([call for call in calls if call[0] == ""], [("", ("pacman",), False)])
        # 请求里只筛了 pacman，就不该偷偷去问玲珑
        self.assertEqual([call for call in calls if call[2]], [])

    def test_installed_flag_comes_from_the_installed_list(self):
        app = self.app()
        calls = []
        installed = [LocalPackage("pacman", "firefox", "133.0-1", "Web browser")]
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])), \
                patch.object(Index, "search", side_effect=self.fake_search(calls)), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=installed):
            payload = app.featured({"sources": "pacman"}, {})
        records = self.records(payload)
        self.assertTrue(records["firefox"]["installed"])
        self.assertFalse(records["code"]["installed"])

    def test_on_demand_sources_are_queried_only_when_missing(self):
        app = self.app()
        calls = []
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])), \
                patch.object(Index, "search", side_effect=self.fake_search(calls)), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=[]):
            payload = app.featured({"sources": "pacman,linyaps"}, {})
        records = self.records(payload)
        wps = records["wps"]
        self.assertTrue(wps["resolved"])
        self.assertEqual(wps["source"], "linyaps")
        self.assertEqual(wps["package"], "cn.wps.wps-office")
        self.assertIn(("cn.wps.wps-office", ("linyaps",), True), calls)
        # 已经在本地索引命中的应用不会再去按需查询
        self.assertNotIn(("firefox", ("linyaps",), True), calls)

    def test_only_on_demand_sources_skips_the_local_scan(self):
        app = self.app()
        calls = []
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])), \
                patch.object(Index, "search", side_effect=self.fake_search(calls)), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=[]):
            payload = app.featured({"sources": "linyaps"}, {})
        self.assertFalse([call for call in calls if call[0] == ""])
        self.assertEqual(payload["resolved"], 1)
        self.assertEqual(self.records(payload)["wps"]["source"], "linyaps")

    def test_search_failures_are_reported(self):
        app = self.app()
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])), \
                patch.object(Index, "search", side_effect=self.fake_search([], ["pacman：坏仓库"])), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=[]):
            payload = app.featured({"sources": "pacman"}, {})
        self.assertEqual(payload["errors"], ["pacman：坏仓库"])
        self.assertEqual(payload["resolved"], 3)

    def test_default_sources_come_from_discovery(self):
        app = self.app()
        with patch("dick.web.discover", return_value=(self.REPOSITORIES, [])) as scan, \
                patch.object(Index, "search", side_effect=self.fake_search([])), \
                patch.object(Settings, "available", return_value=True), \
                patch("dick.web.read_installed", return_value=[]):
            payload = app.featured({}, {})
        self.assertEqual(payload["sources"], ["pacman"])
        self.assertEqual(scan.call_args.kwargs["native"], True)
        with self.assertRaisesRegex(DickError, "未知来源"):
            app.featured({"sources": "pip"}, {})


class WebHttpTests(FixtureTest):
    """HTTP 层：静态资源、路由与错误码（服务只绑定 127.0.0.1 的随机端口）。"""

    def setUp(self):
        super().setUp()
        self.write("etc/dick.toml", "[sources]\nenabled = [\"pacman\", \"linyaps\"]\n")
        settings = Settings(config_path=self.root / "etc/dick.toml", root=self.root,
                            cache_dir=self.root / "cache")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = WebApp(settings, offline=True)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        quiet = patch.object(Handler, "log_message", lambda *arguments: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urlrequest.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urlerror.HTTPError as error:
            try:
                return error.code, error.headers.get("Content-Type"), error.read()
            finally:
                error.close()

    def test_index_and_assets_are_served(self):
        status, content_type, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertIn(b"<html", body)
        for path, expected in (("/assets/app.js", "javascript"), ("/assets/app.css", "css")):
            status, content_type, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, content_type)
            self.assertTrue(body)

    def test_static_assets_keep_the_job_dock_and_hidden_rule(self):
        """任务框能收成右下角胶囊；且样式表必须显式声明 [hidden]，否则 display:flex 会盖掉它。"""
        _, _, home = self.request("/")
        for marker in (b'id="jobdock"', b'id="jobTab"', b'id="jobCollapse"', b'id="jobTabClose"'):
            self.assertIn(marker, home)
        _, _, script = self.request("/assets/app.js")
        for marker in (b"setJobPanel", b"setJobTab", b"toggleJobPanel", b"hideJobDock"):
            self.assertIn(marker, script)
        _, _, styles = self.request("/assets/app.css")
        self.assertIn(b"[hidden] { display: none !important; }", styles)

    def test_static_assets_keep_the_token_gate_and_privilege_prompt(self):
        """令牌门、提权密码框、令牌清洗必须一直待在静态资源里（都是纯前端逻辑）。"""
        _, _, home = self.request("/")
        for marker in (b'id="tokenGate"', b'id="tokenForm"', b'id="tokenInput"', b'id="tokenError"',
                       b'id="passGate"', b'id="passForm"', b'id="passInput"', b'id="passCancel"',
                       b'id="tokenValue"'):
            self.assertIn(marker, home)
        _, _, script = self.request("/assets/app.js")
        for marker in (b"openTokenGate", b"closeTokenGate", b"submitToken", b"askPassword",
                       b"storedToken", b"applyToken", b"X-Dick-Token"):
            self.assertIn(marker, script)
        # 令牌可能被粘成「中文+空格」，而 fetch 的请求头只接受 latin-1：必须过滤掉
        self.assertIn(rb"replace(/[^\x21-\x7e]/g, '')", script)

    def test_static_assets_route_every_icon_through_the_token_helper(self):
        """后端返回的 record.icon 里没有令牌，<img> 又带不了请求头：所有图标都得过 iconSrc。"""
        _, _, script = self.request("/assets/app.js")
        self.assertIn(b"const withToken = (url) =>", script)
        self.assertIn(b"const iconSrc = (record) =>", script)
        for forbidden in (b"src: record.icon", b"src: detail.icon", b"src: candidate.icon",
                          b"image.src = record.icon", b"image.src = iconUrl(pkg.source, pkg.name)"):
            self.assertNotIn(forbidden, script)
        self.assertGreaterEqual(script.count(b"iconSrc("), 6)

    def test_static_assets_keep_the_mobile_layout_from_overflowing(self):
        """手机上侧栏是静态块，而 grid 的 1fr 最小是 auto：一行不换行的导航会把整页撑出屏幕。"""
        _, _, styles = self.request("/assets/app.css")
        self.assertIn(b"@media (max-width: 620px)", styles)
        # 侧栏必须允许收缩，导航在窄屏改成可横向滚动的一行
        self.assertIn(b".sidebar { position: static; height: auto; border-right: 0;"
                      b" border-bottom: 1px solid var(--line); min-width: 0; }", styles)
        self.assertIn(b".nav { flex-direction: row; gap: 6px; overflow-x: auto;"
                      b" padding-bottom: 2px; scrollbar-width: none; }", styles)

    def test_unknown_routes_return_404(self):
        status, _, body = self.request("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("未知接口", json.loads(body)["error"])
        status, _, body = self.request("/assets/../pyproject.toml")
        self.assertEqual(status, 404)

    def test_status_and_translate_over_http(self):
        with patch("dick.web.discover", return_value=([], [])):
            status, content_type, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["family"], "arch")
        self.assertEqual([entry["source"] for entry in payload["sources"]], list(SOURCES))
        status, _, body = self.request("/api/translate", "POST", {"texts": ["hello"]})
        self.assertEqual(status, 400)
        self.assertIn("未配置 AI", json.loads(body)["error"])
        status, _, body = self.request("/api/nope", "POST", {})
        self.assertEqual(status, 404)

    def test_job_and_icon_endpoints(self):
        status, _, body = self.request("/api/job/deadbeef")
        self.assertEqual(status, 400)
        self.assertIn("未知任务", json.loads(body)["error"])
        status, content_type, body = self.request("/api/icon?source=pacman&name=firefox")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/svg+xml")
        self.assertIn(b"<svg", body)
        status, _, body = self.request("/api/icon")
        self.assertEqual(status, 400)
        self.assertIn("icon 需要 name", json.loads(body)["error"])

    def test_featured_over_http(self):
        with patch("dick.web.discover", return_value=([], [])), \
                patch.object(Index, "search", return_value=([], [])):
            status, content_type, body = self.request("/api/featured")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["total"], len(catalog.APPS))
        self.assertEqual(payload["resolved"], 0)
        self.assertEqual([section["id"] for section in payload["sections"]],
                         [category.id for category in catalog.CATEGORIES])
        self.assertTrue(payload["hero"]["key"])
        self.assertFalse(payload["hero"]["resolved"])

    def test_post_body_validation(self):
        status, _, body = self.request("/api/ai", "POST")
        self.assertEqual(status, 400)
        self.assertIn("没有要保存的设置", json.loads(body)["error"])
        request = urlrequest.Request(self.base + "/api/ai", data=b"not-json",
                                     headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urlerror.HTTPError) as wrong_type:
            urlrequest.urlopen(request, timeout=10)
        self.assertEqual(wrong_type.exception.code, 400)
        self.assertIn("Content-Type", json.loads(wrong_type.exception.read())["error"])
        wrong_type.exception.close()
        request = urlrequest.Request(self.base + "/api/ai", data=b"{oops",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urlerror.HTTPError) as broken:
            urlrequest.urlopen(request, timeout=10)
        self.assertEqual(broken.exception.code, 400)
        self.assertIn("合法 JSON", json.loads(broken.exception.read())["error"])
        broken.exception.close()


class SecurityTests(unittest.TestCase):
    """自签证书与访问令牌：只在临时目录里生成文件，不碰真实配置。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tls = self.root / "tls"
        if shutil.which("openssl") is None:
            self.skipTest("没有 openssl")

    def test_certificate_names_cover_local_and_configured_host(self):
        names = certificate_names("192.168.1.20")
        self.assertIn("localhost", names["dns"])
        self.assertIn("127.0.0.1", names["ip"])
        self.assertIn("192.168.1.20", names["ip"])
        self.assertEqual(len(names["dns"]), len(set(names["dns"])))
        for value in names["dns"] + names["ip"]:
            self.assertTrue(value.strip(), "空白的 SAN 会让 openssl 失败")
            self.assertNotIn(" ", value)

    def test_certificate_names_include_every_interface_address(self):
        """装了 VPN 时默认路由可能是隧道地址，网卡地址必须也进 SAN。"""
        local = interface_addresses()
        for address in local:
            self.assertRegex(address, r"^\d+\.\d+\.\d+\.\d+$")
            self.assertNotEqual(address, "0.0.0.0")
        names = certificate_names()
        for address in local:
            if address not in {"0.0.0.0", "127.0.0.1"}:
                self.assertIn(address, names["ip"])

    def test_ensure_certificate_reuses_and_regenerates(self):
        cert, key = ensure_certificate(self.tls, "192.168.1.20")
        self.assertTrue(cert.exists() and key.exists())
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tls.stat().st_mode), 0o700)
        first = cert.read_bytes()
        again, again_key = ensure_certificate(self.tls, "192.168.1.20")
        self.assertEqual(again.read_bytes(), first)
        self.assertEqual(again_key, key)
        moved, _ = ensure_certificate(self.tls, "10.0.0.5")  # 局域网地址变了：重签
        self.assertEqual(moved, cert)
        self.assertNotEqual(cert.read_bytes(), first)

    def test_ssl_context_requires_tls12_and_a_loadable_certificate(self):
        cert, key = ensure_certificate(self.tls, "127.0.0.1")
        context = ssl_context(cert, key)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        with self.assertRaises(DickError):
            ssl_context(self.tls / "没有这个证书", key)

    def test_resolve_token_precedence_and_permissions(self):
        settings = Mock()
        settings.config_path = self.root / "dick.toml"
        settings.web_token = ""
        self.assertEqual(resolve_token(settings, disabled=True), ("", False))
        self.assertEqual(resolve_token(settings, " 命令行的 "), ("命令行的", False))
        settings.web_token = " 配置里的 "
        self.assertEqual(resolve_token(settings), ("配置里的", False))
        settings.web_token = ""
        path = token_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("文件里的\n", encoding="utf-8")
        self.assertEqual(resolve_token(settings), ("文件里的", False))
        path.unlink()
        generated, fresh = resolve_token(settings)
        self.assertTrue(fresh)
        self.assertEqual(len(generated), 12)
        self.assertTrue(generated.isalnum())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), generated)
        self.assertEqual(resolve_token(settings), (generated, False))  # 第二次直接读文件


class PasswordPrivilegeTests(FixtureTest):
    """网页里现填的 sudo 密码：只走 stdin，不落盘、不进日志；错了要有明确反馈。"""

    SUDO = "#!/bin/sh\nread -r password\nif [ \"$password\" = \"hunter2\" ]; then\n" \
           "  echo '已提权，开始干活'\n  exit 0\nfi\necho 'sudo: Sorry, try again.' 1>&2\nexit 1\n"

    def fake_sudo(self):
        """把 PATH 指到一个只有假 sudo 的目录，验证密码真的从 stdin 喂进去了。"""
        directory = self.root / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "sudo"
        script.write_text(self.SUDO, encoding="utf-8")
        script.chmod(0o755)
        return patch.dict(os.environ, {"PATH": f"{directory}:/usr/bin:/bin"})

    def test_password_reaches_sudo_through_stdin(self):
        self.settings.root = Path("/")
        installer = Installer(self.settings, Mock(), lambda line: None, password="hunter2")
        self.assertEqual(installer.sudo_command(["sudo", "pacman", "-S", "htop"])[:4],
                         ["sudo", "-S", "-p", ""])
        lines = []
        installer = Installer(self.settings, Mock(), lines.append, stream=lines.append, password="hunter2")
        with self.fake_sudo():
            self.assertEqual(installer.execute(["sudo", "/bin/sh", "-c", "echo 真命令"]), 0)
        self.assertFalse(installer.auth_failed)
        self.assertIn("已提权，开始干活", lines)
        self.assertNotIn("hunter2", "\n".join(lines))  # 密码绝不进日志

    def test_wrong_password_is_reported_clearly(self):
        self.settings.root = Path("/")
        lines = []
        installer = Installer(self.settings, Mock(), lines.append, stream=lines.append, password="错的")
        with self.fake_sudo():
            self.assertEqual(installer.execute(["sudo", "/bin/sh", "-c", "echo 真命令"]), 1)
        self.assertTrue(installer.auth_failed)
        log = "\n".join(lines)
        self.assertIn("Sorry, try again", log)     # 原始报错照旧原样给出
        self.assertIn("sudo 密码不正确", log)       # 另外补一句人话
        self.assertNotIn("错的", log)               # 密码本身绝不能进日志

    def test_sudo_command_keeps_the_password_off_the_command_line(self):
        installer = Installer(self.settings, Mock(), lambda line: None, password="hunter2")
        command = installer.sudo_command(["sudo", "pacman", "-S", "--", "htop"])
        self.assertEqual(command, ["sudo", "-S", "-p", "", "pacman", "-S", "--", "htop"])
        self.assertNotIn("hunter2", " ".join(command))
        self.assertEqual(installer.sudo_command(["pacman", "-S", "htop"]), ["pacman", "-S", "htop"])

    def test_install_stops_after_a_rejected_password(self):
        """密码不对时换来源也是白搭：只试第一个来源就停，并说清原因。"""
        settings = Mock()
        settings.priority = ["pacman", "aur"]
        settings.available = lambda source: True
        lines = []
        installer = Installer(settings, Mock(), lines.append, dry_run=False, yes=True, password="错的")
        installer.index.search = Mock(return_value=([Package("htop", "pacman")], []))
        installer.install_command = Mock(return_value=["sudo", "pacman", "-S", "htop"])

        def execute(command):
            installer.auth_failed = True
            return 1

        installer.execute = execute
        result = installer.install("htop", ["pacman", "aur"])
        self.assertFalse(result["success"])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(installer.index.search.call_count, 1)
        self.assertIn("不再尝试其它来源", "\n".join(lines))

    def test_install_attempts_snap_last_even_when_configured_first(self):
        """priority.order 把 snap 写在最前面也没用：真正安装时它一定排在最后。"""
        path = self.write("etc/snap-first.toml", '[priority.arch]\norder = ["snap", "pacman", "aur"]\n')
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "snap-cache")
        self.assertEqual(settings.priority, ["pacman", "aur", "snap"])
        settings.available = lambda source: True
        installer = Installer(settings, Mock(), lambda line: None, dry_run=False, yes=True)
        installer.index.search = Mock(side_effect=lambda target, sources, exact: (
            [Package(target, sources[0])], []))
        installer.install_command = lambda package: ["echo", package.source]
        tried = []

        def execute(command):
            tried.append(command[-1])
            return 1  # 前一个来源失败，才会走到下一个

        installer.execute = execute
        result = installer.install("htop", ["snap", "pacman", "aur"])
        self.assertEqual(tried, ["pacman", "aur", "snap"])
        self.assertFalse(result["success"])

    def test_stale_index_detection_matches_real_manager_output(self):
        """pacman/apt 索引过期时的报错都要能认出来；普通冲突等失败不能误判成过期。"""
        installer = Installer(Mock(), Mock(), lambda line: None)
        installer.last_output = [
            "错误：无法从 mirrors.ustc.edu.cn : The requested URL returned error: 404 "
            "获取文件 'mlt-7.40.0-3.1-x86_64_v4.pkg.tar.zst'",
            "错误：无法提交处理 (无法获取某些文件)",
        ]
        self.assertTrue(installer.looks_like_stale_index())
        installer.last_output = ["E: Failed to fetch http://deb.debian.org/... 404  Not Found"]
        self.assertTrue(installer.looks_like_stale_index())
        installer.last_output = ["错误：文件冲突：/usr/bin/htop 已存在"]
        self.assertFalse(installer.looks_like_stale_index())
        installer.last_output = []
        self.assertFalse(installer.looks_like_stale_index())

    def test_stale_index_refreshes_and_retries_once(self):
        """索引过期（镜像 404）时先刷新索引再重试一次，成功了就当没这回事。"""
        settings = Mock()
        settings.priority = ["pacman"]
        settings.available = lambda source: True
        lines = []
        installer = Installer(settings, Mock(), lines.append, dry_run=False, yes=True)
        installer.index.search = Mock(return_value=([Package("krita", "pacman")], []))
        install_command = ["sudo", "pacman", "-S", "--noconfirm", "--", "cachyos-extra-v4/krita"]
        installer.install_command = Mock(return_value=install_command)
        installer.refresh_command = Mock(return_value=["sudo", "pacman", "-Sy"])
        calls = []

        def execute(command):
            calls.append(command)
            if command is install_command and calls.count(install_command) == 1:
                installer.last_output = [
                    "错误：无法从 mirrors.ustc.edu.cn : The requested URL returned error: 404 "
                    "获取文件 'mlt-7.40.0-3.1-x86_64_v4.pkg.tar.zst'",
                ]
                return 1
            installer.last_output = []
            return 0

        installer.execute = execute
        result = installer.install("krita", ["pacman"])
        self.assertTrue(result["success"])
        self.assertEqual(calls, [install_command, ["sudo", "pacman", "-Sy"], install_command])
        self.assertEqual(installer.refresh_command.call_args.args, ("pacman",))
        self.assertTrue(result["attempts"][-1]["refreshed"])
        self.assertIn("先刷新索引再重试一次", "\n".join(lines))

    def test_stale_index_refresh_is_skipped_for_other_failures(self):
        """冲突之类的失败跟镜像无关，不许顺手刷新索引。"""
        settings = Mock()
        settings.priority = ["pacman"]
        settings.available = lambda source: True
        lines = []
        installer = Installer(settings, Mock(), lines.append, dry_run=False, yes=True)
        installer.index.search = Mock(return_value=([Package("htop", "pacman")], []))
        command = ["sudo", "pacman", "-S", "htop"]
        installer.install_command = Mock(return_value=command)
        installer.refresh_command = Mock(return_value=["sudo", "pacman", "-Sy"])
        executed = []

        def execute(entry):
            executed.append(entry)
            installer.last_output = ["错误：文件冲突：/usr/bin/htop 已存在"]
            return 1

        installer.execute = execute
        result = installer.install("htop", ["pacman"])
        self.assertFalse(result["success"])
        installer.refresh_command.assert_not_called()
        self.assertEqual(executed, [command])
        self.assertNotIn("先刷新索引再重试一次", "\n".join(lines))

    def test_stale_index_is_refreshed_at_most_once_per_source(self):
        """刷新之后还是 404 就别再刷了，并提示去做一次完整升级。"""
        settings = Mock()
        settings.priority = ["pacman"]
        settings.available = lambda source: True
        lines = []
        installer = Installer(settings, Mock(), lines.append, dry_run=False, yes=True)
        installer.index.search = Mock(return_value=([Package("krita", "pacman")], []))
        command = ["sudo", "pacman", "-S", "krita"]
        installer.install_command = Mock(return_value=command)
        installer.refresh_command = Mock(return_value=["sudo", "pacman", "-Sy"])
        calls = []

        def execute(entry):
            calls.append(entry)
            if entry == ["sudo", "pacman", "-Sy"]:  # 刷新本身成功，但装还是 404
                installer.last_output = []
                return 0
            installer.last_output = ["错误：无法获取文件 'mlt-7.40.0-3.1-x86_64_v4.pkg.tar.zst'"]
            return 1

        installer.execute = execute
        result = installer.install("krita", ["pacman"])
        self.assertFalse(result["success"])
        self.assertEqual(calls, [command, ["sudo", "pacman", "-Sy"], command])
        self.assertEqual(installer.refresh_command.call_count, 1)
        self.assertIn("「更新与升级」", "\n".join(lines))

    def test_action_hides_the_password_and_rate_limits_guesses(self):
        path = self.write("etc/dick.toml", "")
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache", create=True)
        settings.root = Path("/")  # action 只允许在真实根目录下做系统变更
        app = WebApp(settings, offline=True)
        with patch.object(WebApp, "job_install",
                          return_value={"results": [], "dry_run": False, "auth_failed": True}) as install:
            created = app.action({}, {"action": "install", "targets": ["htop"], "sources": ["pacman"],
                                      "dry_run": False, "confirm": True, "password": "hunter2"})
            self.assertIs(created["request"]["password"], True)
            self.assertNotIn("hunter2", json.dumps(created))
            deadline = time.time() + 10
            while time.time() < deadline and app.job_by_id(created["job"]["id"]).snapshot(0)["status"] == "running":
                time.sleep(0.01)
        self.assertEqual(install.call_args.args[-1], "hunter2")  # 密码确实交给了安装器
        install.assert_called_once()
        for _ in range(5):
            app.note_privilege(False)
        self.assertGreater(app.privilege_wait(), 0)
        with self.assertRaises(DickError) as blocked:
            app.action({}, {"action": "install", "targets": ["htop"], "sources": ["pacman"],
                            "dry_run": False, "confirm": True, "password": "hunter2"})
        self.assertIn("秒后再试", str(blocked.exception))
        app.note_privilege(True)
        self.assertEqual(app.privilege_wait(), 0)
        with self.assertRaises(DickError):
            app.action({}, {"action": "install", "targets": ["htop"], "sources": ["pacman"],
                            "dry_run": False, "confirm": True, "password": "x" * 600})


class TokenHttpTests(FixtureTest):
    """HTTP 层的访问令牌：静态资源放行，接口要 X-Dick-Token 或 ?token=。"""

    def setUp(self):
        super().setUp()
        path = self.write("etc/dick.toml", "")
        settings = Settings(config_path=path, root=self.root, cache_dir=self.root / "cache", create=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = WebApp(settings, offline=True)
        self.server.token = "s3cret-token"
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        quiet = patch.object(Handler, "log_message", lambda *arguments: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, headers=None):
        request = urlrequest.Request(self.base + path, headers=headers or {})
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urlerror.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def test_api_needs_the_token_but_the_shell_does_not(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        for path in ("/assets/app.js", "/assets/app.css"):
            self.assertEqual(self.request(path)[0], 200)
        status, body = self.request("/api/status")
        self.assertEqual(status, 401)
        payload = json.loads(body)
        self.assertEqual(payload["code"], "token")
        self.assertIn("访问令牌", payload["error"])
        self.assertEqual(self.request("/api/icon?source=other&name=htop")[0], 401)

    def test_token_may_come_from_the_header_or_the_query(self):
        status, _ = self.request("/api/status", {"X-Dick-Token": "s3cret-token"})
        self.assertEqual(status, 200)
        status, _ = self.request("/api/status?token=s3cret-token")
        self.assertEqual(status, 200)
        status, _ = self.request("/api/icon?source=other&name=htop&token=s3cret-token")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/status", {"X-Dick-Token": "wrong-token"})[0], 401)
        self.assertEqual(self.request("/api/status?token=")[0], 401)

    def test_disabled_token_lets_everything_through(self):
        self.server.token = ""
        self.assertEqual(self.request("/api/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
