// Minimal ambient typing for the View Transitions API — not present in the
// project's bundled DOM lib version. Only what src/lib/theme.ts relies on.
interface ViewTransition {
  readonly ready: Promise<void>;
  readonly finished: Promise<void>;
  readonly updateCallbackDone: Promise<void>;
  skipTransition(): void;
}

interface Document {
  startViewTransition?(callback: () => void | Promise<void>): ViewTransition;
}
