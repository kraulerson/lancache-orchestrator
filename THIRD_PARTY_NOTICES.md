# Third-party notices

The orchestrator's own code is MIT. These components ship inside its container
image, unmodified, each under its own license. Their license texts are in the
image at `/steam-manifest-helper/licenses/`.

| Component | Version | License | Where | How it is used |
|---|---|---|---|---|
| SteamKit2 | 3.4.0 | LGPL-2.1-only | `/steam-manifest-helper/SteamKit2.dll` | Library called by the Steam manifest helper. Shipped as a separate, replaceable assembly, not merged or trimmed, as the LGPL requires |
| protobuf-net, protobuf-net.Core | 3.2.56 | Apache-2.0 | `/steam-manifest-helper/` | Dependency of SteamKit2; also reads DepotDownloader's saved login once |
| ZstdSharp.Port | 0.8.7 | MIT | `/steam-manifest-helper/` | Dependency of SteamKit2 |
| System.IO.Hashing | 10.0.1 | MIT | `/steam-manifest-helper/` | Dependency of SteamKit2 |
| .NET runtime | 10.0 | MIT | `/steam-manifest-helper/` | Bundled by the self-contained publish |

Python dependencies are checked against an allow-list by `tests/test_licenses.py`.

DepotDownloader (GPL-2.0) shipped in the image as a separate program until #361
replaced it. None of its code is included in the helper.
