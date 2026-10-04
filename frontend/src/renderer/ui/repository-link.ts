import { h, on } from "../dom";
import { repositoryTarget } from "../../shared/repository-target";
// Local GitHub mark: no remote image requests; HF's brand is the hugging-face emoji.
const githubSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38v-1.49c-2.23.48-2.7-1.08-2.7-1.08-.36-.92-.89-1.17-.89-1.17-.73-.5.06-.49.06-.49.8.06 1.22.82 1.22.82.71 1.21 1.87.86 2.33.66.07-.52.28-.86.51-1.06-1.78-.2-3.64-.89-3.64-3.96 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.65 7.65 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.08-1.87 3.75-3.65 3.95.29.25.54.74.54 1.49v2.21c0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>`;
/** Caller owns how to open the URL. Safe provider roots and accessible brand affordance only. */
export function buildRepositoryLink(value: string, label: string, open: (url: string) => void): HTMLAnchorElement | null {
  const target = repositoryTarget(value);
  if (!target) return null;
  const provider = target.provider === "github" ? "GitHub" : "Hugging Face";
  const link = h("a", { class: "repository-link", href: target.url, target: "_blank", rel: "noopener noreferrer",
    title: `${label} · ${provider}`, "aria-label": `${label} · ${provider}`, "data-provider": target.provider });
  if (target.provider === "github") {
    // CSS mask inherits theme ink; no unsafe HTML/SVG parsing.
    const icon = h("span", { class: "repository-link__github", "aria-hidden": "true" });
    icon.style.setProperty("--repository-icon", `url("data:image/svg+xml,${encodeURIComponent(githubSvg)}")`);
    link.append(icon);
  } else link.append(h("span", { class: "repository-link__hf", text: "🤗", "aria-hidden": "true" }));
  on(link, "click", event => { event.preventDefault(); open(target.url); });
  return link;
}
