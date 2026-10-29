#!/usr/bin/env python3
"""upkg - scaffold a GitHub repo that ships a Unity package.

The repo is a small Unity project whose Assets/<PackageFolder>/ directory is the
package root (the folder consumers install via a git URL with ?path=). The
package follows Unity's recommended layout:
https://docs.unity3d.com/Manual/cus-layout.html

Standard library only. Requires `git` and an authenticated `gh` on PATH.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# Every package is named <PACKAGE_DOMAIN>.<name> with code in <NAMESPACE_ROOT>.<Name>.
PACKAGE_DOMAIN = "com.radtaddev"
NAMESPACE_ROOT = "RadTadDev"
INITIAL_VERSION = "0.1.0"
UNITY_HUB_EDITOR_DIR = Path(r"C:\Program Files\Unity\Hub\Editor")


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def _enable_ansi():
    if os.name == "nt":
        os.system("")  # turns on VT processing in the Windows console


def bold(s):
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def info(msg):
    print(f"  {msg}")


def step(msg):
    print(bold(f"\n==> {msg}"))


def die(msg, code=1):
    print(f"\nerror: {msg}", file=sys.stderr)
    sys.exit(code)


def ask(prompt, default=None, validate=None, allow_empty=False):
    """Prompt for a string. `validate` returns an error message or None."""
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        value = raw or (default or "")
        if not value and not allow_empty:
            print("  A value is required.")
            continue
        err = validate(value) if (validate and value) else None
        if err:
            print(f"  {err}")
            continue
        return value


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def choose(prompt, options, default=0):
    """Pick one of `options` (list of (value, label)). Returns the value."""
    print(f"{prompt}")
    for i, (_, label) in enumerate(options, 1):
        marker = "*" if i - 1 == default else " "
        print(f"  {marker}{i:>2}) {label}")
    while True:
        raw = input(f"Select 1-{len(options)} [{default + 1}]: ").strip()
        if not raw:
            return options[default][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  Invalid selection.")


# ---------------------------------------------------------------------------
# External tools
# ---------------------------------------------------------------------------

def run(args, cwd=None, check=True, capture=True):
    proc = subprocess.run(
        args, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
        die(f"command failed: {' '.join(args)}\n{detail}")
    return proc


def gh_json(*endpoint_args):
    proc = run(["gh", "api", *endpoint_args], check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def require_tools(need_gh):
    if not shutil.which("git"):
        die("git was not found on PATH.")
    if need_gh:
        if not shutil.which("gh"):
            die("GitHub CLI (gh) was not found on PATH. Install it from https://cli.github.com/")
        if run(["gh", "auth", "status"], check=False).returncode != 0:
            die("gh is not authenticated. Run `gh auth login` first.")


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

UNITY_FULL_RE = re.compile(r"^\d{4}\.\d+\.\d+[abfp]\d+$")


def split_words(text):
    """'Name Of Package' / 'name-of-package' / 'NameOfPackage' -> ['name', 'of', 'package']."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def derive_names(text):
    words = split_words(text)
    pascal = "".join(w.capitalize() for w in words)
    return {
        "repo": "-".join(words),                                  # name-of-package
        "display_name": " ".join(w.capitalize() for w in words),  # Name Of Package
        "package_id": f"{PACKAGE_DOMAIN}.{''.join(words)}",       # com.radtaddev.nameofpackage
        "folder": pascal,                                         # Assets/NameOfPackage
        "namespace": f"{NAMESPACE_ROOT}.{pascal}",                # RadTadDev.NameOfPackage
    }


def validate_name(text):
    words = split_words(text)
    if not words:
        return "Use letters and digits, e.g. 'Name Of Package'."
    if words[0][0].isdigit():
        return "The name must start with a letter (it becomes a C# namespace)."
    if "unity" in "".join(words):
        return "Unity forbids 'unity' anywhere in third-party package names."
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def installed_unity_editors():
    roots = [UNITY_HUB_EDITOR_DIR]
    secondary = Path(os.environ.get("APPDATA", "")) / "UnityHub" / "secondaryInstallPath.json"
    try:
        extra = json.loads(secondary.read_text(encoding="utf-8"))
        if extra:
            roots.append(Path(extra))
    except (OSError, ValueError):
        pass
    editors = {}
    for root in roots:
        if root.is_dir():
            for p in root.iterdir():
                if p.is_dir() and UNITY_FULL_RE.match(p.name):
                    editors.setdefault(p.name, p)

    def key(v):
        return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", v)]
    return {v: editors[v] for v in sorted(editors, key=key, reverse=True)}


def default_project_manifest(editor_dir):
    """Packages/manifest.json Unity would create for a new project, plus the Test
    Framework (which Unity leaves out, but the generated test assemblies need).
    Returns None if the editor's package metadata can't be read."""
    path = editor_dir / "Editor" / "Data" / "Resources" / "PackageManager" / "Editor" / "manifest.json"
    try:
        packages = json.loads(path.read_text(encoding="utf-8"))["packages"]
    except (OSError, ValueError, KeyError):
        return None
    deps = {name: p["version"] for name, p in packages.items()
            if p.get("isDefault") and "version" in p}
    test_fw = packages.get("com.unity.test-framework", {}).get("version")
    if not deps or not test_fw:
        return None
    deps["com.unity.test-framework"] = test_fw
    return {"dependencies": dict(sorted(deps.items()))}


def gh_owners():
    user = gh_json("user") or {}
    login = user.get("login")
    if not login:
        die("Could not read your GitHub user via `gh api user`.")
    orgs = gh_json("user/orgs", "--paginate") or []
    return user, [login] + sorted((o["login"] for o in orgs), key=str.lower)


def gh_licenses():
    data = gh_json("licenses") or []
    return sorted(((l["key"], l["name"]) for l in data), key=lambda x: x[1].lower())


def license_text(key, year, holder):
    data = gh_json(f"licenses/{key}")
    if not data:
        die(f"Could not fetch license '{key}' from GitHub.")
    body = data["body"]
    for token in ("[year]", "[yyyy]", "<year>"):
        body = body.replace(token, str(year))
    for token in ("[fullname]", "[name of copyright owner]", "<name of author>"):
        body = body.replace(token, holder)
    return data.get("spdx_id") or key, body


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

GITIGNORE = """\
# Unity generated
/[Ll]ibrary/
/[Tt]emp/
/[Oo]bj/
/[Bb]uild/
/[Bb]uilds/
/[Ll]ogs/
/[Uu]ser[Ss]ettings/
/[Mm]emoryCaptures/
/[Rr]ecordings/

# Samples imported into this dev project from the package's Samples~ folder
/[Aa]ssets/[Ss]amples/
/[Aa]ssets/[Ss]amples.meta

# IDEs
.vs/
.vscode/
.idea/
.consulo/
/[Aa]ssets/Plugins/Editor/JetBrains*
*.csproj
*.unityproj
*.sln
*.slnx
*.suo
*.user
*.userprefs
*.pidb
*.booproj
*.svd
*.pdb
*.mdb
*.opendb
*.VC.db
*.pidb.meta
*.pdb.meta
*.mdb.meta

# Builds / misc
*.tmp
*.apk
*.aab
*.app
*.unitypackage
*.unitypackage.meta
ExportedObj/
sysinfo.txt
mono_crash.*
crashlytics-build.properties
.DS_Store
Thumbs.db
"""

def render(template, **values):
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def asmdef(name, root_ns, references=(), editor_only=False, test=False):
    data = {
        "name": name,
        "rootNamespace": root_ns,
        "references": list(references),
        "includePlatforms": ["Editor"] if editor_only else [],
        "excludePlatforms": [],
        "allowUnsafeCode": False,
        "overrideReferences": test,
        "precompiledReferences": ["nunit.framework.dll"] if test else [],
        "autoReferenced": not test,
        "defineConstraints": ["UNITY_INCLUDE_TESTS"] if test else [],
        "versionDefines": [],
        "noEngineReferences": False,
    }
    return json.dumps(data, indent=4) + "\n"


def build_files(cfg):
    """Return {relative_path: content} for the whole repo."""
    pkg = f"Assets/{cfg['folder']}"
    ns = cfg["namespace"]
    asm = cfg["namespace"]
    short = cfg["package_id"].rsplit(".", 1)[-1]
    repo_url = f"https://github.com/{cfg['owner']}/{cfg['repo']}"
    blob = f"{repo_url}/blob/main/{pkg}"
    git_url = f"{repo_url}.git?path=/{pkg}"
    cls = cfg["folder"]
    v = dict(display=cfg["display_name"], ns=ns, cls=cls, package_id=cfg["package_id"],
             git_url=git_url, version=INITIAL_VERSION, unity=cfg["unity_min"],
             editor=cfg["unity_editor"] or cfg["unity_min"], pkg=pkg, short=short,
             date=datetime.date.today().isoformat())

    manifest = {
        "name": cfg["package_id"],
        "version": INITIAL_VERSION,
        "displayName": cfg["display_name"],
        "description": "",
        "unity": cfg["unity_min"],
        "author": {"name": cfg["author_name"], "url": f"https://github.com/{cfg['owner']}"},
        "documentationUrl": f"{repo_url}#readme",
        "changelogUrl": f"{blob}/CHANGELOG.md",
        "licensesUrl": f"{blob}/LICENSE.md",
        "dependencies": {},
        "samples": [{
            "displayName": "Example",
            "description": "Minimal usage example.",
            "path": "Samples~/Example",
        }],
    }
    if cfg["license_spdx"]:
        manifest["license"] = cfg["license_spdx"]

    files = {
        ".gitignore": GITIGNORE,
        "LICENSE": cfg["license_body"],
        "README.md": render(ROOT_README, **v),
        f"{pkg}/package.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        f"{pkg}/README.md": render(PKG_README, **v),
        f"{pkg}/CHANGELOG.md": render(CHANGELOG, **v),
        f"{pkg}/LICENSE.md": cfg["license_body"],
        f"{pkg}/Third Party Notices.md": render(THIRD_PARTY, **v),
        f"{pkg}/Runtime/{asm}.asmdef": asmdef(asm, ns),
        f"{pkg}/Runtime/{cls}Info.cs": render(RUNTIME_CS, **v),
        f"{pkg}/Editor/{asm}.Editor.asmdef": asmdef(f"{asm}.Editor", f"{ns}.Editor", [asm],
                                                    editor_only=True),
        f"{pkg}/Editor/{cls}Menu.cs": render(EDITOR_CS, **v),
        f"{pkg}/Tests/Runtime/{asm}.Tests.asmdef": asmdef(
            f"{asm}.Tests", f"{ns}.Tests",
            ["UnityEngine.TestRunner", "UnityEditor.TestRunner", asm], test=True),
        f"{pkg}/Tests/Runtime/{cls}Tests.cs": render(RUNTIME_TEST_CS, **v),
        f"{pkg}/Tests/Editor/{asm}.Editor.Tests.asmdef": asmdef(
            f"{asm}.Editor.Tests", f"{ns}.Editor.Tests",
            ["UnityEngine.TestRunner", "UnityEditor.TestRunner", asm, f"{asm}.Editor"],
            editor_only=True, test=True),
        f"{pkg}/Tests/Editor/{cls}EditorTests.cs": render(EDITOR_TEST_CS, **v),
        f"{pkg}/Samples~/Example/ExampleUsage.cs": render(SAMPLE_CS, **v),
        f"{pkg}/Documentation~/{short}.md": render(DOCS_MD, **v),
    }
    if cfg["unity_editor"]:
        files["ProjectSettings/ProjectVersion.txt"] = f"m_EditorVersion: {cfg['unity_editor']}\n"
    if cfg["project_manifest"]:
        files["Packages/manifest.json"] = json.dumps(cfg["project_manifest"], indent=2) + "\n"
    return files


# Unity requires .meta files for everything under Assets except '~' folders and
# dotfiles. Shipping them means the package installs cleanly before anyone has
# opened the project in the editor (git packages are immutable, so Unity can't
# generate them on the consumer's side).

META_FOLDER = """\
fileFormatVersion: 2
guid: {guid}
folderAsset: yes
DefaultImporter:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

META_MONO = """\
fileFormatVersion: 2
guid: {guid}
MonoImporter:
  externalObjects: {{}}
  serializedVersion: 2
  defaultReferences: []
  executionOrder: 0
  icon: {{instanceID: 0}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

META_SIMPLE = """\
fileFormatVersion: 2
guid: {guid}
{importer}:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""


def meta_for(path):
    guid = uuid.uuid4().hex
    if path.is_dir():
        return META_FOLDER.format(guid=guid)
    ext = path.suffix.lower()
    if ext == ".cs":
        return META_MONO.format(guid=guid)
    if ext == ".asmdef":
        return META_SIMPLE.format(guid=guid, importer="AssemblyDefinitionImporter")
    if ext in (".md", ".txt", ".json", ".xml", ".yaml", ".bytes", ".csv"):
        return META_SIMPLE.format(guid=guid, importer="TextScriptImporter")
    return META_SIMPLE.format(guid=guid, importer="DefaultImporter")


def write_metas(assets_dir):
    for dirpath, dirnames, filenames in os.walk(assets_dir):
        dirnames[:] = [d for d in dirnames if not d.endswith("~") and not d.startswith(".")]
        here = Path(dirpath)
        entries = [here / d for d in dirnames]
        entries += [here / f for f in filenames
                    if not f.endswith(".meta") and not f.startswith(".")]
        for entry in entries:
            meta = entry.with_name(entry.name + ".meta")
            if not meta.exists():
                meta.write_text(meta_for(entry), encoding="utf-8", newline="\n")


def write_repo(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    write_metas(root / "Assets")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

ROOT_README = """\
# {{display}}

## Installation

In Unity, open **Window > Package Manager**, click **+ > Add package from git URL...** and enter:

```
{{git_url}}
```

To pin a release, append a tag: `{{git_url}}#v{{version}}`

Or add it to `Packages/manifest.json` directly:

```json
{
  "dependencies": {
    "{{package_id}}": "{{git_url}}#v{{version}}"
  }
}
```

Requires Unity {{unity}} or newer.

## Development

This repository is a Unity project. The package itself lives in
[`{{pkg}}`]({{pkg}}) and that folder is the only part consumers install.

1. Open the repository root in Unity {{editor}} (Unity Hub > Add > Add project from disk).
2. Commit the `Packages/` and `ProjectSettings/` folders Unity generates on first open.
3. Run tests from **Window > General > Test Runner**.

Samples live in `{{pkg}}/Samples~` (hidden from Unity). To work on them, import them
through the Package Manager or edit them outside the editor.

## Releasing

1. Bump `version` in [`{{pkg}}/package.json`]({{pkg}}/package.json).
2. Move the `Unreleased` notes in [`CHANGELOG.md`]({{pkg}}/CHANGELOG.md) under the new version.
3. Commit, then tag and push: `git tag v<version> && git push --tags`.
"""

PKG_README = """\
# {{display}}

`{{package_id}}` &middot; see the repository README for installation and development notes.

## Layout

| Folder | Contents |
| --- | --- |
| `Runtime/` | Runtime code (`{{ns}}`) |
| `Editor/` | Editor-only code (`{{ns}}.Editor`) |
| `Tests/Runtime`, `Tests/Editor` | Play mode and edit mode tests |
| `Samples~/` | Importable samples (hidden from Unity) |
| `Documentation~/` | Package documentation (hidden from Unity) |
"""

CHANGELOG = """\
# Changelog

All notable changes to this package will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [{{version}}] - {{date}}

### Added

- Initial package structure.
"""

THIRD_PARTY = """\
# Third Party Notices

This package contains no third-party software.
"""

DOCS_MD = """\
# {{display}}

## Getting started

Describe how to use the package here.
"""

RUNTIME_CS = """\
namespace {{ns}}
{
    public static class {{cls}}Info
    {
        public const string PackageName = "{{package_id}}";
    }
}
"""

EDITOR_CS = """\
using UnityEditor;
using UnityEngine;

namespace {{ns}}.Editor
{
    public static class {{cls}}Menu
    {
        [MenuItem("Tools/{{display}}/About")]
        private static void About()
        {
            Debug.Log($"{{{cls}}Info.PackageName}");
        }
    }
}
"""

RUNTIME_TEST_CS = """\
using NUnit.Framework;

namespace {{ns}}.Tests
{
    public class {{cls}}Tests
    {
        [Test]
        public void PackageNameIsSet()
        {
            Assert.AreEqual("{{package_id}}", {{cls}}Info.PackageName);
        }
    }
}
"""

EDITOR_TEST_CS = """\
using NUnit.Framework;

namespace {{ns}}.Editor.Tests
{
    public class {{cls}}EditorTests
    {
        [Test]
        public void EditorAssemblyLoads()
        {
            Assert.IsNotNull(typeof({{cls}}Menu));
        }
    }
}
"""

SAMPLE_CS = """\
using UnityEngine;

namespace {{ns}}.Samples
{
    public class ExampleUsage : MonoBehaviour
    {
        private void Start()
        {
            Debug.Log($"Using {{{ns}}.{{cls}}Info.PackageName}");
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def pick_unity_editor(requested):
    """Newest installed editor unless one was requested. Returns (version, manifest)."""
    editors = installed_unity_editors()
    if requested:
        if not UNITY_FULL_RE.match(requested):
            die(f"'{requested}' is not a full editor version, e.g. 6000.0.23f1.")
        version = requested
    elif editors:
        version = next(iter(editors))
    else:
        return None, None
    manifest = default_project_manifest(editors[version]) if version in editors else None
    return version, manifest


def gather(args):
    cfg = derive_names(ask("Package name (e.g. Name Of Package)", args.name, validate_name))
    cfg["path"] = Path(os.getcwd()).resolve() / cfg["repo"]
    if cfg["path"].exists():
        die(f"{cfg['path']} already exists.")

    if args.offline:
        user = {"login": "owner", "name": None}
        cfg["owner"] = "owner"
    else:
        user, owners = gh_owners()
        labels = [(o, f"{o} (personal)" if i == 0 else o) for i, o in enumerate(owners)]
        cfg["owner"] = choose("Create the repo under", labels)

    cfg["visibility"] = choose("Visibility", [("private", "Private"), ("public", "Public")])

    licenses = [("mit", "MIT License")] if args.offline else gh_licenses()
    lic_options = licenses + [(None, "None (all rights reserved)")]
    default_lic = next((i for i, (k, _) in enumerate(lic_options) if k == "mit"), 0)
    cfg["license_key"] = choose("License", lic_options, default_lic)

    cfg["unity_editor"], cfg["project_manifest"] = pick_unity_editor(args.unity)
    cfg["unity_min"] = (".".join(cfg["unity_editor"].split(".")[:2])
                        if cfg["unity_editor"] else "2022.3")
    cfg["author_name"] = user.get("name") or cfg["owner"]

    year = datetime.date.today().year
    if cfg["license_key"]:
        if args.offline:
            cfg["license_spdx"] = "MIT"
            cfg["license_body"] = f"MIT License\n\nCopyright (c) {year} {cfg['author_name']}\n"
        else:
            cfg["license_spdx"], cfg["license_body"] = license_text(
                cfg["license_key"], year, cfg["author_name"])
    else:
        cfg["license_spdx"] = None
        cfg["license_body"] = f"Copyright (c) {year} {cfg['author_name']}. All rights reserved.\n"
    return cfg


def summarize(cfg):
    step("Summary")
    rows = [
        ("Repository", f"{cfg['owner']}/{cfg['repo']} ({cfg['visibility']})"),
        ("License", cfg["license_spdx"] or "none"),
        ("Package", cfg["package_id"]),
        ("Display name", cfg["display_name"]),
        ("Package root", f"Assets/{cfg['folder']}"),
        ("Namespace", cfg["namespace"]),
        ("Unity", f"{cfg['unity_editor'] or 'unpinned'} (min {cfg['unity_min']})"),
        ("Local path", str(cfg["path"])),
    ]
    width = max(len(k) for k, _ in rows)
    for k, val in rows:
        info(f"{k:<{width}}  {val}")


def create(cfg, args):
    root = cfg["path"]
    root.mkdir(parents=True)
    write_repo(root, build_files(cfg))
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-m", f"Initial {cfg['display_name']} package structure"], cwd=root)

    if not args.offline:
        step("Creating GitHub repository")
        cmd = ["gh", "repo", "create", f"{cfg['owner']}/{cfg['repo']}", f"--{cfg['visibility']}",
               "--source", ".", "--remote", "origin", "--push"]
        if run(cmd, cwd=root, check=False, capture=False).returncode != 0:
            die(f"gh repo create failed. The local repo is intact at {root}; retry with:\n"
                f"  cd \"{root}\"\n  {subprocess.list2cmdline(cmd)}")

    step("Done")
    if not args.offline:
        url = f"https://github.com/{cfg['owner']}/{cfg['repo']}"
        info(f"Repo:        {url}")
        info(f"Install URL: {url}.git?path=/Assets/{cfg['folder']}")
    info(f"Local:       {root}")
    if not cfg["project_manifest"]:
        info("Add the Test Framework package (Package Manager) so the test assemblies compile.")


def main():
    _enable_ansi()
    parser = argparse.ArgumentParser(
        description="Create a GitHub repo pre-structured as a Unity package.")
    parser.add_argument("name", nargs="?", help='package name, e.g. "Name Of Package"')
    parser.add_argument("--unity", help="editor version to pin (default: newest installed)")
    parser.add_argument("--offline", action="store_true",
                        help="generate and commit locally only; don't call GitHub")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the final confirmation")
    args = parser.parse_args()

    require_tools(need_gh=not args.offline)
    try:
        cfg = gather(args)
        summarize(cfg)
        if not args.yes and not confirm("\nCreate it?", True):
            die("Aborted.", 0)
        create(cfg, args)
    except (KeyboardInterrupt, EOFError):
        die("Aborted.", 130)


if __name__ == "__main__":
    main()
