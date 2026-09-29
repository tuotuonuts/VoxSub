/** Browser-only test entry: real app/store/CSS; no preload, backend, audio or models. */
import "../src/renderer/index";
import { store } from "../src/renderer/store";
import { setLanguage } from "../src/renderer/i18n";
setLanguage(new URLSearchParams(location.search).get("lang") === "en" ? "en" : "zh");
Object.assign(window, { scrollTest: { store } });
