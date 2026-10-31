# Security

Lumen runs entirely on your Mac. It starts a local HTTP server that binds only
to `127.0.0.1`, has no authentication, and checks the `Host` and `Origin` of
every mutating request. It never contacts the network except the optional
`LUMEN_GEOCODE_ONLINE=1` place lookup, which is off by default, and the local
oMLX server you point it at. Camera originals are only ever read.

Do not change the bind address. Anything on a LAN address needs authentication
and transport security that Lumen does not have.

## Reporting a vulnerability

Please report privately through GitHub's **Report a vulnerability** button on
the Security tab of this repository rather than in a public issue. Include the
Lumen version (`lumen --version`), the steps to reproduce, and what an attacker
could do. You will get a reply within a week.

Things that count: a browser page reaching the server from another origin, a
response that leaks a source path, fingerprint or model output, a route that
writes outside the Lumen library, or anything that touches a camera original.
