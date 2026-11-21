# Security

Lumen runs on your Mac. It starts a local HTTP server that binds only to
`127.0.0.1`, has no authentication, and checks the `Host` and `Origin` of
every mutating request. Camera originals are only ever read.

Out of the box it contacts nothing but the local oMLX server you point it at.
Three things reach further, each off until you turn it on:

- The online place lookup (`LUMEN_GEOCODE_ONLINE=1`, or Places and map in
  Settings).
- `lumen map-region`, one map request you run by hand from the command line.
- The AI engine, when Settings has switched it from This Mac to Base (your own
  model server) or Online (a service you hold a key for). Advice requests then
  go to that one address and carry resized previews re-encoded from pixels
  only, with no EXIF, GPS, colour profile or comment, and the prompts.
  Originals, source paths, fingerprints and coordinates are never sent. The
  Base and Online keys are kept in the macOS Keychain, go only to their own
  engine, and are never shown in the browser or in an error message.

Do not change the bind address. Anything on a LAN address needs authentication
and transport security that Lumen does not have.

## Reporting a vulnerability

Please report privately through GitHub's **Report a vulnerability** button on
the Security tab of this repository rather than in a public issue. Include the
Lumen version (`lumen --version`), the steps to reproduce, and what an attacker
could do. You will get a reply within a week.

Things that count: a browser page reaching the server from another origin, a
response that leaks a source path, fingerprint or model output, a route that
writes outside the Lumen library, anything that touches a camera original, a
Base or Online key reaching the browser, a log or another host, or a request
to Base or Online that carries an original, its metadata, a path or a
position.
