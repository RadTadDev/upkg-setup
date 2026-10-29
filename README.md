# upkg-setup

Sets up repos to become Unity packages.

`upkg` scaffolds a Unity project whose `Assets/<PackageFolder>/` is a package in
[Unity's recommended layout](https://docs.unity3d.com/Manual/cus-layout.html), commits it,
and publishes it to GitHub with `gh`.

## Requirements

- Windows, Python 3.8+ (standard library only)
- `git`
- [GitHub CLI](https://cli.github.com/), logged in (`gh auth login`)

## Usage

Add this folder to your `PATH`, then from the directory you want the repo created in:

```
upkg
upkg "Name Of Package"
upkg --unity 6000.0.23f1   # pin a specific editor instead of the newest installed
upkg --offline             # generate + commit locally, skip GitHub
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

## What it creates

```
<repo>/
├── .gitignore, LICENSE, README.md
├── ProjectSettings/ProjectVersion.txt     (pins the editor for Unity Hub)
├── Packages/manifest.json                 (editor defaults + Test Framework)
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
