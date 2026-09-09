/* Masseberegning - build-time configuration.
 *
 * These values are public by design: the API base URL and the identity
 * provider's anon key are both meant to be readable by the browser. Nothing
 * secret belongs in this file, or in any file served from GitHub Pages.
 *
 * The Pages workflow overwrites this file at deploy time from repository
 * variables (see .github/workflows/pages.yml). The values below are the local
 * development defaults, so `python app.py` with SERVE_STATIC=1 works with no
 * edits.
 */
window.MB_CONFIG = {
  /* Empty string means "same origin", which is what local development uses.
   * In production this is the API's own hostname, e.g.
   * "https://api.masseberegning.prosit.no" (no trailing slash). */
  apiBase: '',

  /* Supabase project URL, e.g. "https://abcdefgh.supabase.co" */
  supabaseUrl: '',

  /* Supabase anon/publishable key. Safe to publish. */
  supabaseAnonKey: '',

  /* Which sign-in methods to offer. */
  providers: ['google', 'azure'],
  allowEmailLink: true,
};
