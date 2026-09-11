/* Masseberegning - build-time configuration.
 *
 * These values are public by design: the API base URL and the identity
 * provider's publishable key are both meant to be readable by the browser.
 * Nothing secret belongs in this file, or in any file served from GitHub Pages.
 *
 * The Pages workflow overwrites this file at deploy time from repository
 * variables (see .github/workflows/pages.yml). The values below are the local
 * development defaults, so `python app.py` with SERVE_STATIC=1 works with no
 * edits.
 */
window.MB_CONFIG = {
  /* Empty string means "same origin", which is what local development uses.
   * In production this is the API's own hostname, e.g.
   * "https://masseberegning-api-abc123-xy.a.run.app" (no trailing slash). */
  apiBase: '',

  /* Supabase project URL, e.g. "https://abcdefgh.supabase.co" */
  supabaseUrl: '',

  /* Supabase publishable key - the short "sb_publishable_..." string from
   * Settings > API Keys. Safe to publish.
   *
   * If your project still shows only the legacy keys, the long JWT-style
   * `anon` key starting with "eyJ" works here too: set supabaseAnonKey below
   * instead. Supabase is retiring anon/service_role by the end of 2026, so
   * prefer the publishable key on a new project. */
  supabasePublishableKey: '',

  /* Legacy fallback. Leave empty when supabasePublishableKey is set. */
  supabaseAnonKey: '',

  /* Which sign-in methods to offer. 'azure' is Microsoft. Only list a provider
   * you have actually enabled in Supabase, or its button will fail on click. */
  providers: ['google'],

  /* Requires custom SMTP in Supabase to be useful - the built-in email service
   * is capped at 2 messages per hour. */
  allowEmailLink: false,
};
