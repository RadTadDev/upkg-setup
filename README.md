# upkg-setup

Sets up repos to become Unity packages.

`upkg` asks a few questions, scaffolds a Unity project whose `Assets/<PackageFolder>/`
is a package in [Unity's recommended layout](https://docs.unity3d.com/Manual/cus-layout.html),
commits it, and publishes it to GitHub with `gh`.

## Requirements

- Windows, Python 3.8+ (standard library only)
- `git`
- [GitHub CLI](https://cli.github.com/), logged in (`gh auth login`)

## Usage

Add this folder to your `PATH`, then from the directory you want the repo created in:

```
upkg
upkg MyCoolTool --owner my-org
upkg --offline        # generate + commit locally, skip GitHub
```

Or run it directly with `py upkg.py`.

It prompts for:

- **Repository**: name, owner (your account or any org you belong to), description,
  visibility, license (from GitHub's license list), issues/wiki, topics
- **Package**: display name, package name (`com.company.package`), folder under `Assets/`,
  root namespace, version, Unity editor (detected from Unity Hub) and minimum Unity version,
  author, keywords

## What it creates

```
<repo>/
├── .gitignore, .gitattributes, LICENSE, README.md
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
