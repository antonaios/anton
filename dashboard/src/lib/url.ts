/**
 * URL scheme allowlist for any externally-sourced URL that lands in an
 * `<a href>` or `window.open()`. Only `http:`/`https:` are safe to navigate
 * to; this rejects `javascript:`, `data:`, `vbscript:`, `blob:`, `file:` and
 * anything unparseable.
 *
 * Closes the two latent XSS sinks flagged as #sec-shannon-residuals from the
 * 2026-06-12 Shannon DAST: XSS-VULN-01 (news-provider URL in an `<a href>`,
 * RunResultPanel) and XSS-VULN-02 (chip URL into `window.open`, ChatCanvas).
 */
export function isSafeHttpUrl(raw: string | null | undefined): boolean {
  if (!raw) return false;
  try {
    // Absolute parse only (no base): a relative/protocol-relative string
    // throws → rejected, which is the safe default for these external links.
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

/**
 * Scheme allowlist for the `open-file` chip, which is meant to open a LOCAL
 * file. Returns true only for a parseable `file:` URL — rejecting a
 * server-supplied path that starts with `file:` but smuggles another scheme
 * (`javascript:`, `data:`, …) past the bare `startsWith("file:")` string check.
 * The file-side analogue of `isSafeHttpUrl`; closes the open-file/open-url
 * scheme-check asymmetry flagged as #chip-open-file-scheme (Shannon DAST run #2,
 * XSS-VULN-05).
 */
export function isSafeFileUrl(raw: string | null | undefined): boolean {
  if (!raw) return false;
  try {
    return new URL(raw).protocol === "file:";
  } catch {
    return false;
  }
}
