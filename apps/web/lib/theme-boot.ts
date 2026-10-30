// Shared by the root layout (server) and the theme switch (client).
export const THEME_STORAGE_KEY = "agenteval.theme";

/** Runs before first paint: applies a theme the reader chose, so the page does not flash. */
export const THEME_BOOT_SCRIPT = `try{var t=localStorage.getItem("${THEME_STORAGE_KEY}");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;
