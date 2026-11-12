/** The SDK's own warnings: once each, straight to stderr, never through `console` (whose
 * warn and error the SDK sends as records). */
const seen = new Set<string>();

export function warnOnce(key: string, message: string): void {
  if (seen.has(key)) return;
  seen.add(key);
  try {
    process.stderr.write(`clovelly-watch: ${message}\n`);
  } catch {
    // a closed stderr is not the service's problem
  }
}
