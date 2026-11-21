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


def warn_exit(msg, code=1):
    print(f"\nwarning: {msg}", file=sys.stderr)
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
    if not capture:
        sys.stdout.flush()  # keep our output ahead of the child's when stdout is piped
    proc = subprocess.run(
        args, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
        die(f"command failed: {' '.join(args)}\n{detail}")
    return proc


def git_text(root, *args):
    """stdout of a git command run in `root`, or None if it failed."""
    proc = run(["git", *args], cwd=root, check=False)
    return proc.stdout if proc.returncode == 0 else None


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


def repo_root(command):
    """Top level of the repository the current directory is in."""
    proc = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        die(f"Run `upkg {command}` from inside a repository.")
    return Path(proc.stdout.strip()).resolve()


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

To check locally, run `upkg check` (needs the .NET 10 SDK). `upkg check --fix` applies the
whitespace and style fixes first. Without `upkg`, run the checks directly:

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
3. Commit both in one commit, then run `upkg publish`. It checks the version went up and the
   changelog has the new entry, then tags the commit `v<version>` and pushes it. (By hand:
   `git tag v<version> && git push origin HEAD v<version>`.)
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
    # package.json author and license holder: the org when the repo goes under one, else you.
    if cfg["owner"] == user["login"]:
        cfg["author_name"] = user.get("name") or user["login"]
    else:
        cfg["author_name"] = (gh_json(f"orgs/{cfg['owner']}") or {}).get("name") or cfg["owner"]
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
        ("Author", cfg["author_name"]),
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
    root = repo_root("style")
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
    info("Run `upkg check` to see how the code does against the new rules.")


# ---------------------------------------------------------------------------
# Local style check
# ---------------------------------------------------------------------------

STYLE_PROJECT = "ci/StyleCheck/StyleCheck.csproj"
DOC_PROJECT = "ci/DocCheck/DocCheck.csproj"
# The checks from cs-style's .github/workflows/style.yml, in the same order. Each is
# (title, tool, dotnet commands); every command before the last must succeed, and the last
# one reports the findings.
STYLE_CHECKS = [
    ("Whitespace", "dotnet format whitespace",
     [["format", "whitespace", STYLE_PROJECT, "--verify-no-changes"]]),
    ("Style and naming", "dotnet format style",
     [["format", "style", STYLE_PROJECT, "--verify-no-changes"]]),
    ("Documentation and language version", "DocCheck",
     [["build", DOC_PROJECT, "--nologo", "--verbosity", "quiet", "-p:UseSharedCompilation=false"],
      ["run", "--project", DOC_PROJECT, "--no-build", "--", "."]]),
]
# A finding as dotnet format and DocCheck print it: path(line,col): severity ID: message [project]
FINDING_RE = re.compile(r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) "
                        r"(?P<id>[A-Za-z0-9]+): (?P<msg>.*?)(?: \[[^\]]*\])?$")
# Folders DocCheck skips at any depth (StyleCheck skips a subset). Used to spot what --fix changed.
STYLE_SKIP_DIRS = {".git", "ci", "library", "temp", "logs", "usersettings", "obj", "bin"}


def require_dotnet(root):
    """The checks need the .NET SDK whose version DocCheck targets, the one CI installs."""
    m = re.search(r"<TargetFramework>net(\d+)\.",
                  (root / DOC_PROJECT).read_text(encoding="utf-8", errors="replace"))
    major = m.group(1) if m else None
    hint = (f"Install it with `winget install Microsoft.DotNet.SDK.{major}`, then open a new "
            "terminal." if major else "Install it from https://dot.net, then open a new terminal.")
    if not shutil.which("dotnet"):
        die(f"dotnet was not found on PATH. The style checks need the .NET {major or ''} SDK. "
            f"{hint}".replace("  ", " "))
    if major:
        sdks = run(["dotnet", "--list-sdks"], check=False).stdout.splitlines()
        if not any(line.split(".", 1)[0].strip() == major for line in sdks):
            die(f"The style checks need the .NET {major} SDK, the version CI uses. {hint}")


def dotnet(root, *args):
    """Run dotnet in `root`. Returns (exit code, output lines), stderr included."""
    proc = run(["dotnet", *args], cwd=root, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).splitlines()


def repo_relative(root, path):
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return path


def parse_findings(root, lines):
    """Findings in `lines`, deduplicated and sorted by file and position."""
    found = {}
    for line in lines:
        m = FINDING_RE.match(line.strip())
        if m:
            f = dict(m.groupdict(), path=repo_relative(root, m["path"]),
                     line=int(m["line"]), col=int(m["col"]))
            found[(f["path"], f["line"], f["col"], f["id"], f["msg"])] = f
    return [found[k] for k in sorted(found)]


def format_finding(f):
    return f"{f['path']}({f['line']},{f['col']}): {f['sev']} {f['id']}: {f['msg']}"


def run_check(root, commands):
    """Run one check. Returns (findings, None), or ([], output) when a command failed without
    reporting findings, i.e. the tool itself didn't work."""
    *setup, final = commands
    for args in setup:
        code, lines = dotnet(root, *args)
        if code != 0:
            return [], lines
    code, lines = dotnet(root, *final)
    findings = parse_findings(root, lines)
    return findings, (lines if code != 0 and not findings else None)


def crlf_normalized(root, paths):
    """The subset of `paths` whose CRLF line endings git turns into LF when they're committed
    (eol=lf in .gitattributes, and not already committed with CRLF). CI never sees those."""
    out = git_text(root, "--literal-pathspecs", "ls-files", "-z", "--eol", "--cached", "--others",
                   "--", *paths) or ""
    normalized = set()
    for entry in out.split("\0"):
        details, _, path = entry.partition("\t")
        fields = details.split()
        if (any(f.endswith("eol=lf") for f in fields)
                and not any(f in ("i/crlf", "i/mixed") for f in fields)):
            normalized.add(path)
    return normalized


def cs_snapshot(root):
    """Contents of the repo's .cs files, outside the folders the checks skip."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in STYLE_SKIP_DIRS]
        for name in filenames:
            if name.lower().endswith(".cs"):
                path = Path(dirpath) / name
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def check_command(argv):
    parser = argparse.ArgumentParser(
        prog="upkg check",
        description="Run the cs-style checks from the repo's style workflow (whitespace, style and "
                    "naming, documentation) on the working tree, as CI would after you commit. "
                    "Exits with 1 when there are findings.")
    parser.add_argument("--fix", action="store_true",
                        help="apply the whitespace and style fixes first, then check what's left")
    args = parser.parse_args(argv)

    require_git()
    root = repo_root("check")
    missing = [p for p in (STYLE_PROJECT, DOC_PROJECT) if not (root / p).is_file()]
    if missing:
        die(f"{root} has no {' or '.join(missing)}. Run `upkg style` to add the cs-style tools.")
    require_dotnet(root)
    os.environ.setdefault("DOTNET_NOLOGO", "true")
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "true")

    if args.fix:
        before = cs_snapshot(root)
        # Style first: on Windows its fixes can insert CRLF, which the whitespace pass turns into LF.
        for kind in ("style", "whitespace"):
            step(f"Fixing {kind} (dotnet format {kind})")
            code, lines = dotnet(root, "format", kind, STYLE_PROJECT)
            if code != 0:
                die(f"dotnet format {kind} failed:\n" + "\n".join(lines))
        after = cs_snapshot(root)
        changed = sorted(p for p in after if before.get(p) != after[p])
        for rel in changed:
            info(f"fixed    {rel}")
        info("Review the changes with `git diff`." if changed else "Nothing to fix automatically.")

    results = {}
    crlf = set()
    for title, tool, commands in STYLE_CHECKS:
        step(f"{title} ({tool})")
        findings, error = run_check(root, commands)
        if error is not None:
            info(f"{tool} failed to run:")
            for line in error[-40:]:
                info(f"  {line}")
            results[title] = None
            continue
        eol_paths = sorted({f["path"] for f in findings if f["id"] == "ENDOFLINE"})
        normalized = crlf_normalized(root, eol_paths) if eol_paths else set()
        crlf |= normalized
        findings = [f for f in findings if not (f["id"] == "ENDOFLINE" and f["path"] in normalized)]
        for f in findings:
            info(format_finding(f))
        info(plural(len(findings), "finding") if findings else "clean")
        results[title] = findings

    step("Summary")
    width = max(len(t) for t in results)
    for title, findings in results.items():
        status = ("failed to run" if findings is None
                  else plural(len(findings), "finding") if findings else "clean")
        info(f"{title:<{width}}  {status}")
    if crlf:
        print()
        info(f"{plural(len(crlf), 'file')} with Windows (CRLF) line endings not counted: git "
             "converts them to LF on commit, so CI won't report them."
             + ("" if args.fix else " `upkg check --fix` converts them now."))
        for rel in sorted(crlf):
            info(f"  {rel}")

    failed = [t for t, f in results.items() if f is None]
    found = {t: f for t, f in results.items() if f}
    if not failed and not found:
        print()
        info("All checks pass.")
        return
    fixable = found.keys() & {"Whitespace", "Style and naming"}
    if fixable and not args.fix:
        print()
        info("`upkg check --fix` applies the whitespace and style fixes. Naming and missing docs "
             "are not fixed automatically.")
    elif found:
        print()
        info("Fix these by hand, or suppress them (see Suppressing a finding in the cs-style "
             "README).")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

# SemVer 2.0.0 (https://semver.org), which Unity requires for package versions.
_SEMVER_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({_SEMVER_ID}(?:\.{_SEMVER_ID})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
# Version headings in a Keep a Changelog file, e.g. "## [1.2.0] - 2024-01-31".
CHANGELOG_ENTRY_RE = re.compile(r"^##[ \t]+\[?v?([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)\]?",
                                re.MULTILINE)
RELEASE_TAG_PREFIX = "v"


def semver_key(version):
    """Sort key giving SemVer precedence (build metadata ignored), or None if not SemVer."""
    m = SEMVER_RE.match(version or "")
    if not m:
        return None
    core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m.group(4) is None:
        return core + (1, ())  # a release outranks its pre-releases
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(4).split("."))
    return core + (0, ids)


def tag_version_key(tag):
    """semver_key of a release tag like v1.2.0, or None for any other tag."""
    if not tag.startswith(RELEASE_TAG_PREFIX):
        return None
    return semver_key(tag[len(RELEASE_TAG_PREFIX):])


def changelog_entries(text):
    return set(CHANGELOG_ENTRY_RE.findall(text or ""))


def parse_manifest(text):
    try:
        data = json.loads((text or "").lstrip("﻿"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def find_package(root):
    """Repo-relative path of the package root, Assets/<PackageFolder>."""
    found = sorted(p.parent.relative_to(root).as_posix()
                   for p in root.glob("Assets/*/package.json"))
    if not found:
        die(f"No Assets/<Package>/package.json in {root}. "
            "Run `upkg publish` from a repo made by upkg.")
    if len(found) > 1:
        die(f"Found more than one package ({', '.join(found)}), not sure which to publish.")
    return found[0]


def github_https_url(remote_url):
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)github\.com[:/]"
                 r"([^/]+)/(.+?)(?:\.git)?/?$", remote_url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def publish_command(argv):
    parser = argparse.ArgumentParser(
        prog="upkg publish",
        description="Tag the last commit with the package version and push it. The commit must "
                    "raise the version in package.json and add a CHANGELOG.md entry for it.")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args(argv)

    require_git()
    root = repo_root("publish")

    remotes = (git_text(root, "remote") or "").split()
    if not remotes:
        die(f"`upkg publish` isn't supported in a local repo: {root} has no remote to push to.\n"
            "Create the GitHub repo first (this also pushes your commits), then run it again:\n"
            f"  gh repo create <owner>/{root.name} --private --source . --remote origin --push")

    branch = (git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
    if not branch:
        die("HEAD is detached. Check out the branch to release from, then run `upkg publish`.")
    head = (git_text(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") or "").strip()
    if not head:
        die("Nothing is committed yet.")

    # Push where `git push` would: the branch's upstream, else origin (or the only remote).
    upstream = (git_text(root, "config", f"branch.{branch}.remote") or "").strip()
    if upstream in remotes:
        remote = upstream
        remote_ref = ((git_text(root, "config", f"branch.{branch}.merge") or "").strip()
                      or f"refs/heads/{branch}")
    elif "origin" in remotes or len(remotes) == 1:
        remote = "origin" if "origin" in remotes else remotes[0]
        remote_ref = f"refs/heads/{branch}"
    else:
        die(f"{branch} has no upstream and there are several remotes ({', '.join(remotes)}). "
            f"Push it once with `git push -u <remote> {branch}`, then run `upkg publish` again.")
    remote_branch = remote_ref[len("refs/heads/"):] if remote_ref.startswith("refs/heads/") \
        else remote_ref

    # The tag goes on the last commit, so the package must be fully committed.
    pkg = find_package(root)
    dirty = (git_text(root, "status", "--porcelain", "--untracked-files=all", "--", pkg)
             or "").splitlines()
    if dirty:
        listing = "\n".join(f"  {line}" for line in dirty[:10])
        if len(dirty) > 10:
            listing += f"\n  ... and {len(dirty) - 10} more"
        die(f"{pkg} has uncommitted changes. Commit them first, since publish tags the last "
            f"commit:\n{listing}")

    def at(rev, rel):
        return git_text(root, "cat-file", "blob", f"{rev}:{rel}") if rev else None

    manifest_rel, changelog_rel = f"{pkg}/package.json", f"{pkg}/CHANGELOG.md"
    parent = (git_text(root, "rev-parse", "--verify", "--quiet", "HEAD~1^{commit}")
              or "").strip() or None
    manifest = parse_manifest(at("HEAD", manifest_rel))
    version = manifest.get("version")
    prev_version = parse_manifest(at(parent, manifest_rel)).get("version")
    if not isinstance(version, str) or not version:
        die(f"{manifest_rel} has no version.")
    key = semver_key(version)
    if key is None:
        die(f"Version '{version}' in {manifest_rel} isn't a SemVer version like 1.2.0.")

    # Checks against the previous commit: the version went up and the changelog gained its entry.
    if version == prev_version:
        warn_exit(f"The version is still {version}, the same as in the previous commit. Bump "
                  f"`version` in {manifest_rel}, commit it with its CHANGELOG.md entry, and run "
                  "`upkg publish` again.")
    prev_key = semver_key(prev_version) if isinstance(prev_version, str) else None
    if prev_key is not None and key <= prev_key:
        die(f"The version must increase: the previous commit has {prev_version}, "
            f"this one {version}.")
    changelog = at("HEAD", changelog_rel)
    if version not in changelog_entries(changelog):
        warn_exit(f"{changelog_rel} has no entry for {version}. Add a `## [{version}] - "
                  f"{datetime.date.today().isoformat()}` section, commit it, and run "
                  "`upkg publish` again.")
    if version in changelog_entries(at(parent, changelog_rel)):
        warn_exit(f"{changelog_rel} has no new entry: the {version} entry was already in the "
                  "previous commit. Publish expects the version bump and its changelog entry in "
                  "the last commit.")

    tag = f"{RELEASE_TAG_PREFIX}{version}"
    url = (git_text(root, "remote", "get-url", remote) or "").strip()
    step(f"Checking {remote}")
    proc = run(["git", "ls-remote", remote, remote_ref, "refs/tags/*"], cwd=root, check=False)
    if proc.returncode != 0:
        die(f"Couldn't read {remote} ({url}):\n{(proc.stderr or '').strip()}")
    remote_head, remote_tags = None, {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == remote_ref:
            remote_head = sha
        elif ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/"):]
            if name.endswith("^{}"):
                remote_tags[name[:-3]] = sha  # the commit an annotated tag points at
            else:
                remote_tags.setdefault(name, sha)

    if tag in remote_tags:
        where = "this commit" if remote_tags[tag] == head else f"commit {remote_tags[tag][:7]}"
        warn_exit(f"{tag} is already published on {remote}, at {where}. Bump the version to "
                  "publish a new release.")
    local_tag_commit = (git_text(root, "rev-parse", "--verify", "--quiet",
                                 f"refs/tags/{tag}^{{commit}}") or "").strip() or None
    if local_tag_commit and local_tag_commit != head:
        die(f"A local tag {tag} already exists on commit {local_tag_commit[:7]}, not this one. "
            f"Delete it with `git tag -d {tag}` or bump the version.")
    already_tagged = local_tag_commit == head  # tagged by hand or by an interrupted publish

    local_tags = (git_text(root, "tag", "--list") or "").split()
    releases = [t for t in set(local_tags) | set(remote_tags)
                if t != tag and tag_version_key(t) is not None]
    latest = max(releases, key=tag_version_key, default=None)
    if latest and tag_version_key(latest) >= key:
        die(f"The version must increase: {latest} is already tagged, and {version} isn't higher.")

    if remote_head and remote_head != head and run(
            ["git", "merge-base", "--is-ancestor", remote_head, head],
            cwd=root, check=False).returncode != 0:
        die(f"{remote}/{remote_branch} has commits that aren't in {branch}. Run "
            "`git pull --rebase`, check the release commit is still the last one, and run "
            "`upkg publish` again.")

    step("Publish")
    subject = (git_text(root, "log", "-1", "--format=%s", "HEAD") or "").strip()
    rows = [
        ("Package", f"{manifest.get('name') or '?'} ({pkg})"),
        ("Version", f"{prev_version or 'none'} -> {version} (previous commit -> this one)"),
        ("Last tag", latest or "none"),
        ("Tag", f"{tag} on {head[:7]} \"{subject}\""),
        ("Push to", f"{remote} ({url}), branch {remote_branch}"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, val in rows:
        info(f"{k:<{width}}  {val}")
    other = (git_text(root, "status", "--porcelain", "--", ".", f":(exclude){pkg}")
             or "").splitlines()
    if other:
        info(f"Note: {len(other)} uncommitted change(s) outside {pkg} won't be pushed.")
    if not args.yes and not confirm(f"\nTag {tag} and push it to {remote}?", True):
        die("Aborted.", 0)

    if already_tagged:
        info(f"{tag} is already tagged locally on this commit; pushing it.")
    else:
        display = manifest.get("displayName") or manifest.get("name") or pkg
        run(["git", "tag", "-a", tag, "-m", f"{display} {version}"], cwd=root)

    step(f"Pushing to {remote}")
    # Atomic, so the remote never gets the tag without the branch (or the other way around).
    push = ["git", "push", "--atomic", remote, f"refs/heads/{branch}:{remote_ref}",
            f"refs/tags/{tag}"]
    if run(push, cwd=root, check=False, capture=False).returncode != 0:
        undo = ""
        if not already_tagged:
            run(["git", "tag", "-d", tag], cwd=root, check=False)
            undo = f" and the local {tag} tag was removed"
        die(f"Push failed, so nothing was published{undo}. Fix the problem above and run "
            "`upkg publish` again.")

    step("Done")
    info(f"Published {tag} to {remote}/{remote_branch}.")
    web = github_https_url(url)
    if web:
        info(f"Release:     {web}/tree/{tag}")
        info(f"Install URL: {web}.git?path=/{pkg}#{tag}")


def main():
    _enable_ansi()
    subcommands = {"style": style_command, "check": check_command, "publish": publish_command}
    if sys.argv[1:2] and sys.argv[1] in subcommands:
        try:
            subcommands[sys.argv[1]](sys.argv[2:])
        except (KeyboardInterrupt, EOFError):
            die("Aborted.", 130)
        return
    parser = argparse.ArgumentParser(
        prog="upkg",
        usage="%(prog)s [-h] [--unity UNITY] [--local] [-y] [name]\n"
              "       %(prog)s style [-h] [--ref REF]\n"
              "       %(prog)s check [-h] [--fix]\n"
              "       %(prog)s publish [-h] [-y]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands (run inside a generated repo):\n"
               "  style          re-pull the cs-style rules and CI check\n"
               "  check          run the CI style checks locally (needs the .NET SDK)\n"
               "  publish        tag the last commit with its package version and push it\n"
               "\n"
               "Run `upkg <command> --help` for a command's options.",
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
