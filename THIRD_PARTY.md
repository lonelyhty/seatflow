# Third-party components

The MIT license in this repository covers the original SeatFlow source code. It does not relicense third-party packages, school websites, branding, or browser-delivered JavaScript.

Runtime packages are installed from their upstream distributions:

| Component | Role | Upstream |
| --- | --- | --- |
| HTTPX | HTTP sessions and requests | https://github.com/encode/httpx |
| lxml | HTML parsing | https://github.com/lxml/lxml |
| cryptography | Login transport encryption | https://github.com/pyca/cryptography |
| Playwright | Browser automation and local browser tests | https://github.com/microsoft/playwright-python |
| Node.js (optional) | Execute locally supplied signing modules | https://github.com/nodejs/node |

Their own distributions contain their license notices. Chromium is installed separately by Playwright and is not distributed in this repository.

## Browser-delivered signing modules — not included

`submitVerify.min.js` and `md5.js` used by the upstream reservation page are omitted from both Git and the Python package. The captured `md5.js` identifies Paul Johnston and contributors and a BSD license; the captured `submitVerify.min.js` does not include a redistribution grant. Public availability alone is not treated as such a grant.

The optional adapter accepts locally supplied, hash-verified copies. The [HTTP setup guide](docs/PROTOCOL.md) documents the required filenames and hashes. No account credentials, session cookies, captured pages, upstream JavaScript, or real reservation records are included in the repository.

The small script strings in `seat_reserver/test_public_assets.py` are original, deliberately synthetic test fixtures. They are not implementations of the upstream reservation signature and cannot produce valid reservation requests.
