# upkg-setup

Sets up repos to become Unity packages.

`upkg` scaffolds a Unity project whose `Assets/<PackageFolder>/` is a package in
[Unity's recommended layout](https://docs.unity3d.com/Manual/cus-layout.html), commits it,
and publishes it to GitHub with `gh`.

## Requirements

- Windows, Python 3.8+ (standard library only)
- `git`, with access to the cs-style repo
- [GitHub CLI](https://cli.github.com/), logged in (`gh auth login`); not needed for `upkg style`
- Optional: .NET SDK 8+ to run the style checks locally (see the generated README)

## Usage

Add this folder to your `PATH`, then from the directory you want the repo created in:

```
upkg
upkg "Name Of Package"
upkg --unity 6000.0.23f1   # pin a specific editor instead of the newest installed
upkg --local               # create + commit locally, don't create the GitHub repo yet
upkg style                 # inside a generated repo: re-pull the cs-style rules
```

Or run it directly with `py upkg.py`.

It asks for the package name, the account or org to create the repo under, visibility and
license. Everything else is derived from the name:

| | `Name Of Package` becomes |
| --- | --- |
| GitHub repo | `name-of-package` |
| Display name | `Name Of Package` |
| Package name | `com.radtaddev.nameofpackage` |
| Package folder | `Assets/NameOfPackage` |
| Namespace / assemblies | `RadTadDev.NameOfPackage` |

The `com.radtaddev` / `RadTadDev` prefixes are constants at the top of `upkg.py`. The dev
project is pinned to the newest Unity editor installed through Unity Hub.

## Code style

Every repo gets the C# style baseline from [cs-style](https://github.com/RadTadDev/cs-style)
(`CS_STYLE_REPO` at the top of `upkg.py`): `.editorconfig`, `Directory.Build.props`,
`.gitattributes`, `.vscode/extensions.json`, the `ci/` tools and the
`.github/workflows/style.yml` check, which runs on every push and pull request once the repo is
on GitHub. Findings are warnings and never block a merge. The generated code passes it.

To pick up rule changes mid-project, run from anywhere inside the repo:

```
upkg style
upkg style --ref v2        # pull a specific cs-style branch or tag (remembered for next time)
```

It overwrites the cs-style files with the latest version, deletes files cs-style no longer has,
and lists what changed for you to review and commit. `.cs-style.json` records the commit and
files that were applied. In repos made before this existed, it also adds `.gitignore`
exceptions so the CI's `.csproj` files and `.vscode/extensions.json` get committed. Because
`style` is a subcommand, a package literally named "Style" has to be entered at the prompt.

## What it creates

```
<repo>/
├── .gitignore, LICENSE, README.md
├── .editorconfig, .gitattributes, Directory.Build.props, .cs-style.json   (cs-style)
├── .github/workflows/style.yml, .vscode/extensions.json, ci/            (cs-style)
├── ProjectSettings/
│   ├── ProjectVersion.txt                 (pins the editor for Unity Hub)
│   ├── ProjectSettings.asset              (from the editor's Hub template: Input System, etc.)
│   └── PackageManagerSettings.asset       (avoids a Package Manager error on first open)
├── Packages/manifest.json                 (editor defaults, Input System, uGUI, IDE support,
│                                           Test Framework)
└── Assets/
    └── <PackageFolder>/                   (package root, installed via ?path=)
        ├── package.json
        ├── README.md, CHANGELOG.md, LICENSE.md, Third Party Notices.md
        ├── Runtime/   <Ns>.asmdef, <Name>Info.cs
        ├── Editor/    <Ns>.Editor.asmdef, <Name>Menu.cs
        ├── Tests/
        │   ├── Runtime/  <Ns>.Tests.asmdef, <Name>Tests.cs
        │   └── Editor/   <Ns>.Editor.Tests.asmdef, <Name>EditorTests.cs
        ├── Samples~/Example/ExampleUsage.cs
        └── Documentation~/<package>.md
```

`.meta` files (with fresh GUIDs) are generated for everything under `Assets/` except the
`~` folders, so the package installs from git before anyone opens the project in Unity.

Consumers install with:

```
https://github.com/<owner>/<repo>.git?path=/Assets/<PackageFolder>
```
