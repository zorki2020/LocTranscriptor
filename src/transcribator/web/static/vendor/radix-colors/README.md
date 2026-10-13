# Radix Colors (vendored)

Color scales from [Radix Colors](https://github.com/radix-ui/colors), MIT License
(see `LICENSE`). The files are copied unchanged from the npm package.

- Package: `@radix-ui/colors` 3.0.0 (published 2023-10-02)
- Source: https://registry.npmjs.org/@radix-ui/colors/-/colors-3.0.0.tgz
- Tarball sha256: `f6cec1ff1fcc58a45eeefc25d0e6aa1f1659845dbd198963617a86e43d21f3d1`
- Tarball integrity (npm): `sha512-FUOsGBkHrYJwCSEtWRCIfQbZG7q1e6DgxCIOe1SUQzDe/7rXXeA47s8yCn6fuTNQAj1Zq4oTFi9Yjp3wzElcxg==`

Scales in use: `sand` (neutral, with alpha), `indigo` (accent, with alpha), `red`,
`green`, `amber`; each in light and dark.

How they are loaded: `templates/base.html` links the light files unconditionally and
the dark files with `media="(prefers-color-scheme: dark)"`, before `app.css`. The dark files declare their
variables on `.dark-theme`, so `<html>` always carries that class: in a light system
appearance the dark files are not applied at all, in a dark one they override the light
values. `app.css` maps the scales to its own semantic tokens; components never use the
scale variables directly.

To update: download the tarball at an exact version, check its integrity against the
registry, replace these files and update the version and hashes above.
