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
import tarfile
import tempfile
import uuid
from pathlib import Path

# Every package is named <PACKAGE_DOMAIN>.<name> with code in <NAMESPACE_ROOT>.<Name>.
PACKAGE_DOMAIN = "com.radtaddev"
NAMESPACE_ROOT = "RadTadDev"
INITIAL_VERSION = "0.1.0"
UNITY_HUB_EDITOR_DIR = Path(r"C:\Program Files\Unity\Hub\Editor")

# Shared C# style rules and CI check, copied into every repo and refreshed by `upkg style`.
CS_STYLE_REPO = "https://github.com/RadTadDev/cs-style.git"
# Files in cs-style that describe that repo itself rather than being part of the baseline.
CS_STYLE_SKIP = {".git", ".gitignore", "README.md", "LICENSE"}
# Records which cs-style commit and files a repo has, so `upkg style` can remove files dropped upstream.
CS_STYLE_STAMP = ".cs-style.json"


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


def require_git():
    if not shutil.which("git"):
        die("git was not found on PATH.")


def require_tools():
    require_git()
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


# Packages a Unity Hub template project gets on top of the editor's built-in modules
# (minus template content like URP and Timeline). Test Framework is what the generated
# test assemblies need; Input System matches the template's input setting below.
DEV_PROJECT_PACKAGES = (
    "com.unity.ide.rider",
    "com.unity.ide.visualstudio",
    "com.unity.inputsystem",
    "com.unity.test-framework",
    "com.unity.ugui",
)


def default_project_manifest(editor_dir):
    """Packages/manifest.json for a new project: the editor's default modules plus
    DEV_PROJECT_PACKAGES at the versions this editor recommends. Returns None if
    the editor's package metadata can't be read."""
    path = editor_dir / "Editor" / "Data" / "Resources" / "PackageManager" / "Editor" / "manifest.json"
    try:
        packages = json.loads(path.read_text(encoding="utf-8"))["packages"]
    except (OSError, ValueError, KeyError):
        return None
    deps = {name: p["version"] for name, p in packages.items()
            if p.get("isDefault") and "version" in p}
    for name in DEV_PROJECT_PACKAGES:
        version = packages.get(name, {}).get("version")
        if not version:
            return None
        deps[name] = version
    return {"dependencies": dict(sorted(deps.items()))}


def template_player_settings(editor_dir, company, product):
    """ProjectSettings.asset from the editor's bundled Unity Hub template, so new
    projects get the same player settings as one made in Hub (e.g. the Input
    System instead of the deprecated Input Manager). Returns None if unavailable."""
    templates = editor_dir / "Editor" / "Data" / "Resources" / "PackageManager" / "ProjectTemplates"
    candidates = sorted(templates.glob("com.unity.template.*.tgz"),
                        key=lambda p: ("3d-cross-platform" not in p.name, "3d" not in p.name))
    member = "package/ProjectData~/ProjectSettings/ProjectSettings.asset"
    for tgz in candidates:
        try:
            with tarfile.open(tgz) as archive:
                text = archive.extractfile(member).read().decode("utf-8")
        except (OSError, KeyError, AttributeError, tarfile.TarError):
            continue
        for key, value in (("productGUID", uuid.uuid4().hex),
                           ("companyName", company), ("productName", product)):
            text = re.sub(rf"(?m)^(  {key}: ).*$", lambda m: m.group(1) + value, text)
        return text
    return None


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

# Build output from the cs-style CI tools in ci/
bin/
obj/

# IDEs (extensions.json comes from cs-style)
.vs/
.vscode/*
!.vscode/extensions.json
.idea/
.consulo/
/[Aa]ssets/Plugins/Editor/JetBrains*
*.csproj
!/ci/**/*.csproj
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

# Unity writes this during a project's first session. Without it, the first open throws a
# NullReferenceException in the Package Manager window's scoped-registry sidebar.
PACKAGE_MANAGER_SETTINGS = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &1
MonoBehaviour:
  m_ObjectHideFlags: 61
  m_CorrespondingSourceObject: {fileID: 0}
  m_PrefabInstance: {fileID: 0}
  m_PrefabAsset: {fileID: 0}
  m_GameObject: {fileID: 0}
  m_Enabled: 1
  m_EditorHideFlags: 0
  m_Script: {fileID: 13964, guid: 0000000000000000e000000000000000, type: 0}
  m_Name:
  m_EditorClassIdentifier:
  m_EnablePreviewPackages: 0
  m_EnablePackageDependencies: 0
  m_AdvancedSettingsExpanded: 1
  m_ScopedRegistriesSettingsExpanded: 1
  oneTimeWarningShown: 0
  m_Registries:
  - m_Id: main
    m_Name:
    m_Url: https://packages.unity.com
    m_Scopes: []
    m_IsDefault: 1
    m_Capabilities: 7
  m_UserSelectedRegistryName:
  m_UserAddingNewScopedRegistry: 0
  m_RegistryInfoDraft:
    m_ErrorMessage:
    m_Original:
      m_Id:
      m_Name:
      m_Url:
      m_Scopes: []
      m_IsDefault: 0
      m_Capabilities: 0
    m_Modified: 0
    m_Name:
    m_Url:
    m_Scopes:
    -
    m_SelectedScopeIndex: 0
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
    v = dict(display=cfg["display_name"], ns=ns, package_id=cfg["package_id"],
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
        f"{pkg}/Editor/{asm}.Editor.asmdef": asmdef(f"{asm}.Editor", f"{ns}.Editor", [asm],
                                                    editor_only=True),
        f"{pkg}/Tests/Runtime/{asm}.Tests.asmdef": asmdef(
            f"{asm}.Tests", f"{ns}.Tests",
            ["UnityEngine.TestRunner", "UnityEditor.TestRunner", asm], test=True),
        f"{pkg}/Tests/Editor/{asm}.Editor.Tests.asmdef": asmdef(
            f"{asm}.Editor.Tests", f"{ns}.Editor.Tests",
            ["UnityEngine.TestRunner", "UnityEditor.TestRunner", asm, f"{asm}.Editor"],
            editor_only=True, test=True),
        f"{pkg}/Documentation~/{short}.md": render(DOCS_MD, **v),
    }
    if cfg["unity_editor"]:
        files["ProjectSettings/ProjectVersion.txt"] = f"m_EditorVersion: {cfg['unity_editor']}\n"
    if cfg["project_manifest"]:
        files["Packages/manifest.json"] = json.dumps(cfg["project_manifest"], indent=2) + "\n"
    if cfg["player_settings"]:
        files["ProjectSettings/ProjectSettings.asset"] = cfg["player_settings"]
    if cfg["unity_editor"]:
        files["ProjectSettings/PackageManagerSettings.asset"] = PACKAGE_MANAGER_SETTINGS
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
# Code style (cs-style)
# ---------------------------------------------------------------------------

def _rmtree(path):
    """shutil.rmtree that also removes git's read-only object files on Windows."""
    def on_error(func, p, _):
        os.chmod(p, 0o700)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=on_error)


def fetch_cs_style(ref=None):
    """Shallow-clone cs-style. Returns (commit, {relative_path: bytes})."""
    tmp = Path(tempfile.mkdtemp(prefix="upkg-cs-style-"))
    try:
        cmd = ["git", "clone", "--quiet", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        run(cmd + [CS_STYLE_REPO, str(tmp / "repo")])
        src = tmp / "repo"
        commit = run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()
        files = {}
        for dirpath, dirnames, filenames in os.walk(src):
            here = Path(dirpath)
            if here == src:
                dirnames[:] = [d for d in dirnames if d not in CS_STYLE_SKIP]
                filenames = [f for f in filenames if f not in CS_STYLE_SKIP]
            for f in filenames:
                files[(here / f).relative_to(src).as_posix()] = (here / f).read_bytes()
        return commit, dict(sorted(files.items()))
    finally:
        _rmtree(tmp)


def read_stamp(root):
    try:
        return json.loads((root / CS_STYLE_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def apply_cs_style(root, commit, files, ref=None):
    """Write the cs-style files into `root`, delete ones a previous version had but this
    one doesn't, and update the stamp. Returns (added, changed, removed) path lists."""
    old = set((read_stamp(root) or {}).get("files", []))
    added, changed = [], []
    for rel, data in files.items():
        path = root / rel
        if not path.exists():
            added.append(rel)
        elif path.read_bytes() != data:
            changed.append(rel)
        else:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    removed = sorted(old - set(files))
    for rel in removed:
        path = root / rel
        if path.is_file():
            path.unlink()
        # drop directories the removal left empty, up to the repo root
        parent = path.parent
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    stamp = {"source": CS_STYLE_REPO, "ref": ref, "commit": commit, "files": list(files)}
    (root / CS_STYLE_STAMP).write_text(json.dumps(stamp, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    return added, changed, removed


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

To ship samples, put them in `{{pkg}}/Samples~` (hidden from Unity) and list each one under
`samples` in `package.json`. Import them through the Package Manager to work on them.

## Code style

C# style rules come from [cs-style](https://github.com/RadTadDev/cs-style): `.editorconfig`,
`Directory.Build.props`, the `ci/` tools and `.github/workflows/style.yml`. Every push and pull
request runs a non-blocking style check that reports findings as warnings. To pull the latest
rules, run `upkg style` from the repository and commit the result. Edits to those files are
overwritten on the next update.

To check locally (needs the .NET SDK):

```
dotnet format whitespace ci/StyleCheck/StyleCheck.csproj --verify-no-changes
dotnet format style ci/StyleCheck/StyleCheck.csproj --verify-no-changes
dotnet run --project ci/DocCheck/DocCheck.csproj -- .
```

Drop `--verify-no-changes` to apply fixes (run `style` before `whitespace`). Naming and missing
docs are not auto-fixed.

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

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def pick_unity_editor(requested):
    """Newest installed editor unless one was requested. Returns (version, editor_dir);
    editor_dir is None when that version isn't installed."""
    editors = installed_unity_editors()
    if requested:
        if not UNITY_FULL_RE.match(requested):
            die(f"'{requested}' is not a full editor version, e.g. 6000.0.23f1.")
        return requested, editors.get(requested)
    if editors:
        return next(iter(editors.items()))
    return None, None


def gather(args):
    cfg = derive_names(ask("Package name (e.g. Name Of Package)", args.name, validate_name))
    cfg["path"] = Path(os.getcwd()).resolve() / cfg["repo"]
    if cfg["path"].exists():
        die(f"{cfg['path']} already exists.")

    user, owners = gh_owners()
    labels = [(o, f"{o} (personal)" if i == 0 else o) for i, o in enumerate(owners)]
    cfg["owner"] = choose("Create the repo under", labels)
    if gh_json(f"repos/{cfg['owner']}/{cfg['repo']}") is not None:
        die(f"https://github.com/{cfg['owner']}/{cfg['repo']} already exists. "
            "Pick another name or owner, or delete that repo first.")

    cfg["visibility"] = choose("Visibility", [("private", "Private"), ("public", "Public")])

    lic_options = gh_licenses() + [(None, "None (all rights reserved)")]
    default_lic = next((i for i, (k, _) in enumerate(lic_options) if k == "mit"), 0)
    cfg["license_key"] = choose("License", lic_options, default_lic)

    cfg["unity_editor"], editor_dir = pick_unity_editor(args.unity)
    cfg["unity_min"] = (".".join(cfg["unity_editor"].split(".")[:2])
                        if cfg["unity_editor"] else "2022.3")
    cfg["author_name"] = user.get("name") or cfg["owner"]
    cfg["project_manifest"] = default_project_manifest(editor_dir) if editor_dir else None
    cfg["player_settings"] = (template_player_settings(editor_dir, NAMESPACE_ROOT,
                                                       cfg["display_name"])
                              if editor_dir else None)

    # Fetched before confirming so a network or access problem stops us before anything is written.
    cfg["cs_style"] = fetch_cs_style()

    year = datetime.date.today().year
    if cfg["license_key"]:
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
        ("Code style", f"cs-style @ {cfg['cs_style'][0][:7]}"),
        ("Local path", str(cfg["path"])),
    ]
    width = max(len(k) for k, _ in rows)
    for k, val in rows:
        info(f"{k:<{width}}  {val}")


def create(cfg, args):
    root = cfg["path"]
    root.mkdir(parents=True)
    write_repo(root, build_files(cfg))
    apply_cs_style(root, *cfg["cs_style"])
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-m", f"Initial {cfg['display_name']} package structure"], cwd=root)

    url = f"https://github.com/{cfg['owner']}/{cfg['repo']}"
    cmd = ["gh", "repo", "create", f"{cfg['owner']}/{cfg['repo']}", f"--{cfg['visibility']}",
           "--source", ".", "--remote", "origin", "--push"]
    if not args.local:
        step("Creating GitHub repository")
        if run(cmd, cwd=root, check=False, capture=False).returncode != 0:
            die(f"gh repo create failed. The local repo is intact at {root}; retry with:\n"
                f"  cd \"{root}\"\n  {subprocess.list2cmdline(cmd)}")

    step("Done")
    info(f"Local:       {root}")
    if args.local:
        info("To publish to GitHub later, run from that folder:")
        info(f"  {subprocess.list2cmdline(cmd)}")
    else:
        info(f"Repo:        {url}")
        info(f"Install URL: {url}.git?path=/Assets/{cfg['folder']}")
    if not cfg["project_manifest"]:
        info("Add the Test Framework package (Package Manager) so the test assemblies compile.")


def unignore_cs_style(root, paths):
    """Repos made by older upkg versions ignore *.csproj and .vscode/, which would keep
    cs-style files out of git. Append exceptions to .gitignore for any that are ignored."""
    def ignored(candidates):
        proc = run(["git", "check-ignore", "--no-index", *candidates], cwd=root, check=False)
        return proc.stdout.splitlines()

    files = ignored(paths)
    if not files:
        return
    # Git can't re-include a file inside an ignored directory, so re-include the directory
    # and ignore its other contents instead (.vscode/ -> !/.vscode/ + /.vscode/*).
    parents = sorted({str(Path(p).parent.as_posix()) + "/" for p in files} - {"./"})
    lines = []
    for d in ignored(parents) if parents else []:
        lines += [f"!/{d}", f"/{d}*"]
    lines += [f"!/{p}" for p in files]
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n# cs-style files (see upkg style)\n" + "".join(f"{line}\n" for line in lines)
    gitignore.write_text(text, encoding="utf-8", newline="\n")
    info(f"Un-ignored in .gitignore: {', '.join(files)}")
    still = ignored(files)
    if still:
        info(f"Still ignored, fix .gitignore by hand: {', '.join(still)}")


def style_command(argv):
    parser = argparse.ArgumentParser(
        prog="upkg style",
        description="Re-pull the cs-style rules and CI check into the current repo.")
    parser.add_argument("--ref", help="cs-style branch or tag to pull (default: its default branch, "
                                      "or the ref this repo was last updated from)")
    args = parser.parse_args(argv)

    require_git()
    proc = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        die("Run `upkg style` from inside a repository.")
    root = Path(proc.stdout.strip()).resolve()
    stamp = read_stamp(root)
    if stamp is None and not (root / "Assets").is_dir():
        if not confirm(f"{root} doesn't look like a upkg repo. Add cs-style to it anyway?", False):
            die("Aborted.", 0)
    ref = args.ref or (stamp or {}).get("ref")

    step(f"Fetching cs-style{f' ({ref})' if ref else ''}")
    commit, files = fetch_cs_style(ref)
    previous = (stamp or {}).get("commit")
    info(f"{previous[:7] if previous else 'none'} -> {commit[:7]}")

    added, changed, removed = apply_cs_style(root, commit, files, ref)
    unignore_cs_style(root, list(files))
    step("Done")
    if not (added or changed or removed):
        info("Already up to date.")
        return
    for label, paths in (("added", added), ("updated", changed), ("removed", removed)):
        for rel in paths:
            info(f"{label:<8} {rel}")
    info("Review and commit the changes, e.g.:")
    info(f'  git add -A && git commit -m "Update cs-style to {commit[:7]}"')


def main():
    _enable_ansi()
    if sys.argv[1:2] == ["style"]:
        try:
            style_command(sys.argv[2:])
        except (KeyboardInterrupt, EOFError):
            die("Aborted.", 130)
        return
    parser = argparse.ArgumentParser(
        epilog="Run `upkg style` inside a generated repo to re-pull the cs-style rules.",
        description="Create a GitHub repo pre-structured as a Unity package.")
    parser.add_argument("name", nargs="?", help='package name, e.g. "Name Of Package"')
    parser.add_argument("--unity", help="editor version to pin (default: newest installed)")
    parser.add_argument("--local", action="store_true",
                        help="create and commit the repo locally; don't create it on GitHub")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the final confirmation")
    args = parser.parse_args()

    require_tools()
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
