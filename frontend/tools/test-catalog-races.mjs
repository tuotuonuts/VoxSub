#!/usr/bin/env node
/**
 * Deterministic behavior tests for the real model-catalog page's async model/cache loads.
 * Only the IPC boundary and DOM are replaced; catalog logic and its store are production code.
 */
import { installMiniDom } from "./mini-dom.mjs";
import { createReporter, importShared } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const { document, window } = dom;
const { check, finish } = createReporter("catalog out-of-order response behavior");
const pendingUnhandled = [];
process.on("unhandledRejection", (reason) => pendingUnhandled.push(reason));

const catalog = await importShared("tools/test-catalog-entry.ts", { bundle: true });
const modelRequests = [];
const cacheRequests = [];
const installRequests = [];
const prepareRequests = [];
const eventListeners = new Set();

window.voxsub = {
  backend: {
    start: async () => ({ ok: true }),
    onEvent: (handler) => {
      eventListeners.add(handler);
      return () => eventListeners.delete(handler);
    },
    command: (command, args) => {
      if (command === catalog.CMD.listModels) {
        return new Promise((resolve, reject) => modelRequests.push({ args, resolve, reject }));
      }
      if (command === catalog.CMD.ocrCacheDir) {
        return new Promise((resolve, reject) => cacheRequests.push({ args, resolve, reject }));
      }
      if (command === catalog.CMD.prepareModelDownload) {
        prepareRequests.push(args);
        return Promise.resolve({ok:true,data:{model_id:args.model_id,download:{
          modelId:args.model_id,token:`fixture-${prepareRequests.length}`,revision:prepareRequests.length,
          completed:0,total:100,status:"queued",stage:"等待下载",source:args.source,
        }}});
      }
      if (command === catalog.CMD.installModel) {
        installRequests.push(args);
        return Promise.resolve({ ok: false, error: "simulated download refusal" });
      }
      if (command === catalog.CMD.state) {
        return Promise.resolve({ ok: true, data: { running: false, paused: false, mode: "a" } });
      }
      return Promise.resolve({ ok: false, error: `unexpected test command: ${command}` });
    },
  },
};

const settle = () => dom.flushAsync(8);
const disconnected = catalog.connectBackend();
for (const listener of [...eventListeners]) listener({ type: "ready" });
await settle();

function newLayer() {
  const layer = document.createElement("div");
  layer.className = "catalog-test-layer";
  document.body.append(layer);
  return layer;
}

function fixtureModel(id, task, name) {
  return {
    id,
    name,
    task,
    quality: 80,
    sizeLabel: "1 MB",
    sizeBytes: 1024,
    installedBytes: 512,
    installed: true,
    builtin: false,
    runtime: "fixture-runtime",
    license: "test-only",
    languages: "en",
    description: `${name} fixture`,
    gpuSupported: false,
    igpuSupported: false,
    npuSupported: false,
    minRamGb: 1,
  };
}

function resolveModels(request, modelsRoot, models) {
  request.resolve({ ok: true, data: { models, modelsRoot, lookupRoots: [], diagnostics: [] } });
}

function resolveCache(request, path) {
  request.resolve({ ok: true, data: { path } });
}

function isModel(handle, name) {
  return [...handle.element.querySelectorAll(".cell__name")].some((node) => node.textContent === name);
}

function isFilterActive(handle, label) {
  return [...handle.element.querySelectorAll(".filter-chip")].some((chip) =>
    chip.textContent === label && chip.classList.contains("is-active") && chip.getAttribute("aria-pressed") === "true",
  );
}

function storeRoots() {
  const state = catalog.store.get();
  return { modelsRoot: state.modelsRoot, cacheRoot: state.cacheRoot };
}

// A's model response remains in flight while B loads and renders first.
const aLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/request-A";
const aHandle = catalog.buildModelCatalog();
aLayer.append(aHandle.element);
await settle();
const requestA = modelRequests.find((request) => request.args?.models_root === "C:/request-A");
check("page A issued its model-list request", Boolean(requestA), JSON.stringify(modelRequests.map((request) => request.args)));
aHandle.dispose();
aLayer.remove();

const bLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/request-B";
const bHandle = catalog.buildModelCatalog();
bLayer.append(bHandle.element);
await settle();
const requestB = modelRequests.find((request) => request.args?.models_root === "C:/request-B");
resolveModels(requestB, "C:/catalog-B", [fixtureModel("model-b", "translate", "Model B")]);
await settle();
const cacheB = cacheRequests.at(-1);
resolveCache(cacheB, "C:/cache-B");
await settle();
check(
  "B response renders B's list and updates its shared roots",
  isModel(bHandle, "Model B") &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/catalog-B", cacheRoot: "C:/cache-B" }),
  JSON.stringify({ dom: bHandle.element.textContent, roots: storeRoots() }),
);
const translateChip = [...bHandle.element.querySelectorAll(".filter-chip")]
  .find((chip) => chip.textContent === "翻译");
translateChip?.click();
check(
  "B can set its own filter before A completes",
  isFilterActive(bHandle, "翻译") && bHandle.element.querySelectorAll(".cell").length === 1,
  bHandle.element.textContent,
);
resolveModels(requestA, "C:/catalog-A", [fixtureModel("model-a", "asr", "Model A")]);
await settle();
aHandle.dispose(); // A's old handle must not clear B's module-level DOM references.
check(
  "late A model response and old dispose leave B's DOM, filter, list and store unchanged",
  isModel(bHandle, "Model B") && !isModel(bHandle, "Model A") &&
    isFilterActive(bHandle, "翻译") &&
    bHandle.element.querySelector(".catalog__count")?.textContent === "1 项" &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/catalog-B", cacheRoot: "C:/cache-B" }) &&
    cacheRequests.length === 1,
  JSON.stringify({ dom: bHandle.element.textContent, roots: storeRoots(), cacheRequests: cacheRequests.length }),
);
bHandle.dispose();
bLayer.remove();

// A's model list can finish while A is current and leave its cache request pending;
// after B2 becomes current, A's delayed cache completion/rejection must not write the store.
const oldCacheLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/cache-A-request";
const oldCacheHandle = catalog.buildModelCatalog();
oldCacheLayer.append(oldCacheHandle.element);
await settle();
const oldCacheModelRequest = modelRequests.find((request) => request.args?.models_root === "C:/cache-A-request");
resolveModels(oldCacheModelRequest, "C:/catalog-cache-A", [fixtureModel("model-cache-a", "translate", "Cache A")]);
await settle();
const oldCacheRequest = cacheRequests.at(-1);
oldCacheHandle.dispose();
oldCacheLayer.remove();

const b2Layer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/cache-B-request";
const b2Handle = catalog.buildModelCatalog();
b2Layer.append(b2Handle.element);
await settle();
const b2ModelRequest = modelRequests.find((request) => request.args?.models_root === "C:/cache-B-request");
resolveModels(b2ModelRequest, "C:/catalog-cache-B", [fixtureModel("model-cache-b", "translate", "Cache B")]);
await settle();
const b2CacheRequest = cacheRequests.at(-1);
resolveCache(b2CacheRequest, "C:/cache-B");
await settle();
const unhandledBeforeCacheFailure = pendingUnhandled.length;
oldCacheRequest.reject(new Error("late stale cache failure"));
await settle();
await new Promise((resolve) => setImmediate(resolve));
check(
  "late cache rejection from disposed A is handled without touching B2",
  pendingUnhandled.length === unhandledBeforeCacheFailure && isModel(b2Handle, "Cache B") &&
    !isModel(b2Handle, "Cache A") &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/catalog-cache-B", cacheRoot: "C:/cache-B" }),
  JSON.stringify({ unhandled: pendingUnhandled.map(String), roots: storeRoots(), dom: b2Handle.element.textContent }),
);
b2Handle.dispose();
b2Layer.remove();

// A separate old-page cache request succeeds late; its successful payload must be just as stale-safe.
const lateCacheLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/late-cache-success-A";
const lateCacheHandle = catalog.buildModelCatalog();
lateCacheLayer.append(lateCacheHandle.element);
await settle();
const lateCacheModel = modelRequests.find((request) => request.args?.models_root === "C:/late-cache-success-A");
resolveModels(lateCacheModel, "C:/late-cache-success-A-root", [fixtureModel("late-cache-a", "translate", "Late cache A")]);
await settle();
const lateCacheRequest = cacheRequests.at(-1);
lateCacheHandle.dispose();
lateCacheLayer.remove();

const latestCacheLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/latest-cache-success-B";
const latestCacheHandle = catalog.buildModelCatalog();
latestCacheLayer.append(latestCacheHandle.element);
await settle();
const latestCacheModel = modelRequests.find((request) => request.args?.models_root === "C:/latest-cache-success-B");
resolveModels(latestCacheModel, "C:/latest-cache-success-B-root", [fixtureModel("latest-cache-b", "translate", "Latest cache B")]);
await settle();
const latestCache = cacheRequests.at(-1);
resolveCache(latestCache, "C:/latest-cache-B");
await settle();
resolveCache(lateCacheRequest, "C:/late-cache-A");
await settle();
check(
  "late successful cache response from a disposed page cannot overwrite the active page or store",
  isModel(latestCacheHandle, "Latest cache B") && !isModel(latestCacheHandle, "Late cache A") &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/latest-cache-success-B-root", cacheRoot: "C:/latest-cache-B" }),
  JSON.stringify({ roots: storeRoots(), dom: latestCacheHandle.element.textContent }),
);
latestCacheHandle.dispose();
latestCacheLayer.remove();

// Refresh the same active page three times, complete the newest request first,
// then complete an older success and reject another older request.
const samePageLayer = newLayer();
document.documentElement.dataset["modelsRoot"] = "C:/same-page";
const samePageHandle = catalog.buildModelCatalog();
samePageLayer.append(samePageHandle.element);
await settle();
[...samePageHandle.element.querySelectorAll(".filter-chip")]
  .find((chip) => chip.textContent === "全部")?.click();
const requestBase = modelRequests.length - 1;
const refreshOne = catalog.loadModels();
const refreshTwo = catalog.loadModels();
await settle();
const [olderSuccess, olderFailure, newest] = modelRequests.slice(requestBase, requestBase + 3);
resolveModels(newest, "C:/same-page-latest", [fixtureModel("model-latest", "asr", "Latest model")]);
await settle();
const newestCache = cacheRequests.at(-1);
resolveCache(newestCache, "C:/same-page-cache");
await settle();
check("latest same-page request normally renders its new model and roots", isModel(samePageHandle, "Latest model") &&
  JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/same-page-latest", cacheRoot: "C:/same-page-cache" }),
  JSON.stringify({ dom: samePageHandle.element.textContent, roots: storeRoots() }),
);
resolveModels(olderSuccess, "C:/same-page-old", [fixtureModel("model-old", "asr", "Old model")]);
const unhandledBeforeModelFailure = pendingUnhandled.length;
olderFailure.reject(new Error("late stale model-list failure"));
await settle();
await new Promise((resolve) => setImmediate(resolve));
await Promise.all([refreshOne, refreshTwo]);
check(
  "same-page older success and late rejection cannot overwrite the latest DOM or store",
  pendingUnhandled.length === unhandledBeforeModelFailure && isModel(samePageHandle, "Latest model") &&
    !isModel(samePageHandle, "Old model") &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/same-page-latest", cacheRoot: "C:/same-page-cache" }),
  JSON.stringify({ unhandled: pendingUnhandled.map(String), roots: storeRoots(), dom: samePageHandle.element.textContent }),
);

// A current refresh failure must preserve the last successful catalogue, even
// after changing a filter forces the in-memory model list to render again.
const cacheCountBeforeFailure = cacheRequests.length;
const currentFailure = catalog.loadModels();
await settle();
modelRequests.at(-1).reject(new Error("current refresh failed"));
await settle();
if (cacheRequests.length > cacheCountBeforeFailure) {
  resolveCache(cacheRequests.at(-1), "C:/same-page-cache");
}
await currentFailure;
[...samePageHandle.element.querySelectorAll(".filter-chip")]
  .find((chip) => chip.textContent === "全部")?.click();
check(
  "current refresh failure preserves the last successful list and roots",
  isModel(samePageHandle, "Latest model") &&
    JSON.stringify(storeRoots()) === JSON.stringify({ modelsRoot: "C:/same-page-latest", cacheRoot: "C:/same-page-cache" }),
  JSON.stringify({ dom: samePageHandle.element.textContent, roots: storeRoots() }),
);

const rejectedRefresh = catalog.loadModels();
await settle();
modelRequests.at(-1).resolve({ ok: false, delivery: "response", error: "catalog unavailable" });
await rejectedRefresh;
check("failed backend envelope also preserves the successful list", isModel(samePageHandle, "Latest model"));

const emptyRefresh = catalog.loadModels();
await settle();
resolveModels(modelRequests.at(-1), "C:/empty-catalog", []);
await settle();
resolveCache(cacheRequests.at(-1), "C:/empty-cache");
await emptyRefresh;
check("successful empty response clears old models rather than retaining stale entries",
  !isModel(samePageHandle, "Latest model") &&
    samePageHandle.element.querySelector(".catalog__count")?.textContent === "0 项" &&
    storeRoots().modelsRoot === "C:/empty-catalog");

// A real catalog download button must carry the selected region to IPC.
document.documentElement.dataset["modelsRoot"] = "C:/dual-source";
const sourceRefresh = catalog.loadModels();
await settle();
resolveModels(modelRequests.at(-1), "C:/dual-source", [
  { ...fixtureModel("asr-moonshine-tiny-en-v2", "asr", "Moonshine"), installed: false },
]);
await settle();
resolveCache(cacheRequests.at(-1), "C:/dual-source-cache");
await sourceRefresh;
const sourceSelect = samePageHandle.element.querySelector("[data-download-source]");
check("catalog offers auto overseas and mainland sources", sourceSelect &&
  [...sourceSelect.querySelectorAll("option")].map(o => o.value).join(",") === "auto,global,china");
for (const source of ["china", "global", "auto"]) {
  sourceSelect.value = source;
  sourceSelect.dispatchEvent(new Event("change"));
  catalog.store.patch({downloads:{}});
  samePageHandle.element.querySelector(".filter-chip").click();
  const download = [...samePageHandle.element.querySelectorAll("button")].find(b => b.textContent === "下载");
  download.click();
  await settle();
  const args = installRequests.at(-1);
  check(`download button forwards ${source} preference`, prepareRequests.at(-1)?.source === source && args?.token === `fixture-${prepareRequests.length}` &&
    args?.model_id === "asr-moonshine-tiny-en-v2" && args?.models_root === "C:/dual-source", JSON.stringify(args));
}

samePageHandle.dispose();
samePageLayer.remove();
disconnected();
dom.restore();
finish();
