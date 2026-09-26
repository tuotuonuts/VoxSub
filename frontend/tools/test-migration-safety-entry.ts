// Match renderer/index.ts's real translator registration without starting its UI.
export * from "./test-migration-entry";
import { setUiTranslator } from "../src/renderer/store";
import { tr } from "../src/renderer/i18n";
setUiTranslator(tr);
