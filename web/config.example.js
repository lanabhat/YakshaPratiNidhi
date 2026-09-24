// Copy to config.js (git-ignored) and fill in for your deployment.
window.LIPI_CONFIG = {
  API_BASE: "http://127.0.0.1:8200",
  AUTH_MODE: "dev", // "dev" (type an email, local only) | "firebase" (real Google sign-in)
  // Firebase console -> your project -> Project settings -> General -> "Your apps" -> the web app's
  // SDK setup snippet. Only used when AUTH_MODE is "firebase" - see README "Admins, roles and rights".
  // Not a traditional secret (Firebase web keys are meant to be public - see Firebase's own docs),
  // but kept out of the repo like the rest of this project's per-deployment config.
  FIREBASE_CONFIG: {
    apiKey: "",
    authDomain: "",
    projectId: "",
  },
};
