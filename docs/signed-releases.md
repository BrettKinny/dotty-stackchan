---
title: Signed Releases
description: GPG-signing release artifacts for supply-chain trust — how to sign locally, how to verify, how to wire it into the firmware-release GitHub Actions workflow, and where the maintainer key fingerprint is published.
---

# Signed releases

## Why GPG-sign release artifacts

When the StackChan firmware (or a server-side `.tar.gz`) is downloaded from
GitHub Releases, the only thing standing between the user and a compromised
binary is whatever trust they place in the source. GPG signatures collapse
that trust into a single, verifiable cryptographic check:

- **Tamper detection.** A signature mismatch immediately surfaces a binary
  that has been altered after the maintainer signed it.
- **Provenance.** A valid signature against the published maintainer
  fingerprint proves the artifact came from someone who controls that key.
- **Recoverable trust.** If the GitHub release infrastructure ever served the
  wrong file (account compromise, mirror hijack), users with the maintainer
  fingerprint locally cached can still detect it.

This is **LEVEL-2 polish** — the goal is to make signing *possible* and
*documented* today, even if every release does not get signed. As soon as the
maintainer key exists, signatures become opt-in for users who care.

## Maintainer key fingerprint

The maintainer's GPG public-key fingerprint is published in [`KEYS.txt`](../KEYS.txt)
at the repo root. This file is the single source of truth — the README links
to it rather than embedding the fingerprint, so the fingerprint never goes
stale across docs.

> **Placeholder:** the current `KEYS.txt` ships with `<FINGERPRINT_PENDING>`.
> The maintainer fills in the real fingerprint on first use. Until then, no
> Dotty release artifact has a verifiable signature.

## Signing a release locally

The minimal path. Requires a GPG keypair on the signing host.

```bash
# Detached, ASCII-armoured signature alongside the binary.
gpg --detach-sign --armor stack-chan.bin
# → produces stack-chan.bin.asc
```

Sign the SHA256SUMS.txt as well — that lets a verifier check every artifact
in one go:

```bash
gpg --detach-sign --armor SHA256SUMS.txt
# → produces SHA256SUMS.txt.asc
```

Both `.bin.asc` and `SHA256SUMS.txt.asc` get attached to the GitHub Release
alongside the binaries.

## Verifying a release (user side)

```bash
# 1. Fetch the maintainer's public key.
gpg --keyserver keys.openpgp.org --recv-keys <MAINTAINER_KEY_FINGERPRINT>

# 2. Verify the signature against the artifact.
gpg --verify stack-chan.bin.asc stack-chan.bin

# Expected output:
#   gpg: Good signature from "<Maintainer Name> <email@example.com>"
#   Primary key fingerprint: <MAINTAINER_KEY_FINGERPRINT>
```

If `gpg --verify` reports `BAD signature`, **do not flash the firmware** —
treat the artifact as compromised and report it to the maintainer.

If `gpg --verify` reports `Can't check signature: No public key`, the local
keyring does not have the maintainer key yet — re-run the `--recv-keys` step.

## GitHub Actions integration

The signing step belongs inside `.github/workflows/firmware-release.yml`,
between "Generate SHA256 checksums" and "Create GitHub Release."

```yaml
- name: Import GPG signing key
  if: ${{ secrets.GPG_PRIVATE_KEY != '' }}
  run: |
    echo "${{ secrets.GPG_PRIVATE_KEY }}" | gpg --batch --import
    echo "default-key ${GPG_KEY_ID}" >> ~/.gnupg/gpg.conf
  env:
    GPG_KEY_ID: ${{ secrets.GPG_KEY_ID }}

- name: Sign artifacts
  if: ${{ secrets.GPG_PRIVATE_KEY != '' }}
  working-directory: firmware/firmware/build
  run: |
    for f in stack-chan.bin ota_data_initial.bin generated_assets.bin SHA256SUMS.txt; do
      gpg --batch --yes --pinentry-mode loopback \
          --passphrase "${{ secrets.GPG_PASSPHRASE }}" \
          --detach-sign --armor "$f"
    done
```

Add the `.asc` files to the `files:` block of the `softprops/action-gh-release`
step so they ship with the release. The `if:` guards mean the workflow keeps
working when secrets are not yet configured — the build proceeds, just
unsigned.

### Required repository secrets

| Secret             | Source                                                                 |
|--------------------|------------------------------------------------------------------------|
| `GPG_PRIVATE_KEY`  | `gpg --armor --export-secret-keys <fingerprint>` on the signing host   |
| `GPG_PASSPHRASE`   | The passphrase used when generating the key                            |
| `GPG_KEY_ID`       | The short or long key ID (`gpg --list-secret-keys --keyid-format LONG`) |

Set them in **Settings → Secrets and variables → Actions** on the GitHub repo.

## Publishing the public key

Three places publish the same fingerprint, redundantly so a tampered copy in
one place is contradicted by the others:

1. **`KEYS.txt`** at the repo root — primary source of truth.
2. **README.md** — one-line "Verifying releases" pointer at `KEYS.txt`
   (fingerprint not duplicated; that goes stale).
3. **A public keyserver** — `keys.openpgp.org` is the recommended choice
   (verified-uploads only, GDPR-compliant identity stripping).

```bash
# Maintainer: publish your key once.
gpg --keyserver keys.openpgp.org --send-keys <MAINTAINER_KEY_FINGERPRINT>
```

## Cross-references

- [`COMPATIBILITY.md`](../COMPATIBILITY.md#release-process) — when a release
  is cut, signing becomes part of the cutting process.
- [`docs/sbom.md`](sbom.md) — sister scaffold; signed SBOMs let a verifier
  cross-check the signed binary against an audited dependency tree.
- [`SECURITY.md`](../SECURITY.md) — threat model the signing scaffold
  defends against.

## Follow-ups

- Generate the maintainer keypair and replace `<FINGERPRINT_PENDING>` in
  `KEYS.txt`.
- Configure repo secrets and uncomment the signing step in
  `firmware-release.yml`.
- Sign at least one tagged release end-to-end to validate the user-side
  verification flow.
- Consider [`sigstore`](https://www.sigstore.dev/) / `cosign` keyless signing
  as a complementary path — no maintainer key to lose, OIDC-rooted trust.
