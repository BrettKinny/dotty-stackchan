// Tailwind config for the Dotty bridge dashboard.
// Used to build a vendored bridge/static/tailwind.min.css so we can
// drop the Play CDN script and add SRI to the stylesheet link.
//
// Re-run with:
//   npx -y -p tailwindcss@3.4.17 -p daisyui@4.12.14 tailwindcss \
//     -c bridge/tailwind.config.js \
//     -i bridge/static/tailwind-input.css \
//     -o bridge/static/tailwind.min.css \
//     --minify
module.exports = {
  content: [
    "bridge/templates/**/*.html",
  ],
  // Classes that are constructed dynamically in the SSE / perception JS
  // in dashboard.html (lines ~476-613) — Tailwind's content scanner
  // can't see string concatenation, so safelist them explicitly.
  safelist: [
    "badge-success", "badge-error", "badge-warning", "badge-info",
    "alert-success", "alert-error", "alert-warning", "alert-info",
  ],
  theme: { extend: {} },
  plugins: [require("daisyui")],
  daisyui: {
    themes: ["light", "dark"],
    logs: false,
  },
};
