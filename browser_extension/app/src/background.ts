import type {
  AdvancedFeatureKey,
  DesktopRequestResult,
  GenericTaskSummary,
  PopupStatePayload,
  PopupView,
} from "./shared/types";
import { createDesktopBridge } from "./background/desktop-bridge";
import { createFeatureBridge } from "./background/feature-bridge";
import { createMediaBridge } from "./background/media-bridge";
import { createResourceBridge } from "./background/resource-bridge";
import {
  INTERCEPT_DOWNLOADS_KEY,
} from "./background/constants";
import {
  cancelDownload,
  eraseDownloadFromHistory,
  getTab,
  localStorageGet,
} from "./background/chrome-helpers";
import {
  getOnSendHeadersExtraInfoSpec,
  supportsDownloadDeterminingFilename,
} from "./shared/browser";

const desktopBridge = createDesktopBridge();
const resourceBridge = createResourceBridge({
  sendDesktopRequest: (payload) => desktopBridge.sendRequest(payload),
});
const featureBridge = createFeatureBridge();
const mediaBridge = createMediaBridge();

let interceptDownloads = true;

function taskCounters(tasks: GenericTaskSummary[]) {
  return {
    total: tasks.length,
    active: tasks.filter((task) => task.status !== "completed").length,
    completed: tasks.filter((task) => task.status === "completed").length,
  };
}

async function buildPopupState(options: {
  preferredTabId?: number | null;
  currentView?: PopupView;
} = {}): Promise<PopupStatePayload> {
  const resolvedTabId = await resourceBridge.resolveActiveTabId(options.preferredTabId ?? null);
  const activeTab = resolvedTabId != null ? await getTab(resolvedTabId) : null;
  const desktopState = desktopBridge.buildSnapshot();
  const resourceState = resourceBridge.buildPopupStateData(resolvedTabId, activeTab);

  const mediaPanelState = await mediaBridge.buildPanelState(
    options.currentView === "advanced" ? resolvedTabId : null,
  );

  return {
    connectionState: desktopState.connectionState,
    connectionMessage: desktopState.connectionMessage,
    desktopVersion: desktopState.desktopVersion,
    token: desktopState.token,
    serverUrl: desktopState.serverUrl,
    interceptDownloads,
    tasks: desktopState.tasks,
    taskCounters: taskCounters(desktopState.tasks),
    tabId: resolvedTabId,
    featureStates: featureBridge.createFeatureStateMap(resolvedTabId),
    mediaItems: mediaPanelState.mediaItems,
    mediaPlaybackState: mediaPanelState.playbackState,
    ...resourceState,
  };
}

async function initialize() {
  const localState = await localStorageGet<{
    [INTERCEPT_DOWNLOADS_KEY]: boolean;
  }>({
    [INTERCEPT_DOWNLOADS_KEY]: true,
  });

  interceptDownloads = Boolean(localState[INTERCEPT_DOWNLOADS_KEY] ?? true);

  await desktopBridge.loadPersistentState();
  await resourceBridge.loadPersistentState();
  await featureBridge.loadPersistentState();
  await resourceBridge.resolveActiveTabId();

  if (desktopBridge.buildSnapshot().token) {
    void desktopBridge.connect();
  }

  desktopBridge.ensureReconnectAlarm();
}

chrome.alarms.onAlarm.addListener((alarm) => {
  desktopBridge.handleReconnectAlarm(alarm);
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "HeartBeat") {
    port.postMessage("HeartBeat");
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local") {
    return;
  }
  desktopBridge.syncLocalStorageChanges(changes);
  if (changes[INTERCEPT_DOWNLOADS_KEY]) {
    interceptDownloads = Boolean(changes[INTERCEPT_DOWNLOADS_KEY].newValue ?? true);
  }
});

chrome.tabs.onActivated.addListener((activeInfo) => {
  void resourceBridge.setLastActiveTab(activeInfo.tabId);
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    return;
  }
  void resourceBridge.refreshActiveTabFromBrowser();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  resourceBridge.handleTabRemoved(tabId);
  mediaBridge.handleTabRemoved(tabId);
  featureBridge.handleTabRemoved(tabId);
});

chrome.webNavigation.onCommitted.addListener((details) => {
  resourceBridge.handleNavigationCommitted(details);
  featureBridge.handleNavigationCommitted(details);
});

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    resourceBridge.handleRequestHeaders(details);
    void resourceBridge.captureRequestResource(details);
  },
  { urls: ["<all_urls>"] },
  getOnSendHeadersExtraInfoSpec(),
);

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    void resourceBridge.captureNetworkResource(details);
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"],
);

async function interceptBrowserDownload(
  downloadItem: chrome.downloads.DownloadItem,
  options: { eraseFromHistory?: boolean } = {},
) {
  const finalUrl = downloadItem.finalUrl || downloadItem.url;
  if (!interceptDownloads || !desktopBridge.isReady() || !/^https?:/i.test(finalUrl)) {
    return;
  }

  try {
    await cancelDownload(downloadItem.id);
    if (options.eraseFromHistory) {
      await eraseDownloadFromHistory(downloadItem.id);
    }
  } catch {
    // Ignore cancellation cleanup failures; the browser download may continue as fallback.
  }

  await resourceBridge.handoffBrowserDownload(downloadItem);
}

function reply(sendResponse: (response?: unknown) => void, response: Promise<unknown>) {
  void response.then(sendResponse);
  return true;
}

if (supportsDownloadDeterminingFilename()) {
  chrome.downloads.onDeterminingFilename.addListener((downloadItem, suggest) => {
    suggest();
    void interceptBrowserDownload(downloadItem);
  });
} else if (chrome.downloads.onCreated?.addListener) {
  chrome.downloads.onCreated.addListener((downloadItem) => {
    void interceptBrowserDownload(downloadItem, { eraseFromHistory: true });
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object") {
    return;
  }

  if (message.Message === "addMedia" && typeof message.url === "string") {
    void resourceBridge.capturePageResource(sender, {
      url: message.url,
      href: message.href,
      mime: message.mime,
      ext: message.extraExt,
      requestHeaders: message.requestHeaders,
    });
    sendResponse("ok");
    return true;
  }

  if (typeof message.type !== "string") {
    return;
  }

  if (message.type === "bridge_page_command") {
    void featureBridge.handleBridgeScriptCommand(message.payload, sender);
    sendResponse({ ok: true });
    return true;
  }

  if (message.type === "popup_get_state") {
    return reply(sendResponse, buildPopupState({
      preferredTabId: typeof message.tabId === "number" ? message.tabId : null,
      currentView: message.view as PopupView | undefined,
    }));
  }

  if (message.type === "popup_set_token") {
    return reply(sendResponse, (async () => {
      await desktopBridge.setToken(String(message.token ?? "").trim());
      return buildPopupState({ currentView: message.view as PopupView | undefined });
    })());
  }

  if (message.type === "popup_set_server_url") {
    return reply(sendResponse, (async () => {
      await desktopBridge.setServerUrl(String(message.serverUrl ?? ""));
      return buildPopupState({ currentView: message.view as PopupView | undefined });
    })());
  }

  if (message.type === "popup_refresh_connection") {
    return reply(sendResponse, (async () => {
      await desktopBridge.connect(true);
      return buildPopupState({ currentView: message.view as PopupView | undefined });
    })());
  }

  if (message.type === "popup_set_intercept_downloads") {
    return reply(sendResponse, (async () => {
      interceptDownloads = Boolean(message.enabled);
      await chrome.storage.local.set({ [INTERCEPT_DOWNLOADS_KEY]: interceptDownloads });
      return buildPopupState({ currentView: message.view as PopupView | undefined });
    })());
  }

  if (message.type === "popup_task_action") {
    return reply(sendResponse, (async () => {
      try {
        return await desktopBridge.sendRequest<DesktopRequestResult>({
          type: "task_action",
          taskId: message.taskId,
          action: message.action,
        });
      } catch (error) {
        return {
          ok: false,
          message: error instanceof Error ? error.message : "任务操作失败",
        };
      }
    })());
  }

  if (message.type === "popup_send_resource") {
    return reply(sendResponse, resourceBridge.sendResource(String(message.resourceId ?? "")));
  }

  if (message.type === "popup_merge_resources") {
    return reply(sendResponse, (async () => {
      const resourceIds = Array.isArray(message.resourceIds)
        ? message.resourceIds.map((value: unknown) => String(value ?? "")).filter(Boolean)
        : [];
      return resourceBridge.mergeResources(resourceIds);
    })());
  }

  if (message.type === "popup_toggle_feature") {
    return reply(sendResponse, (async () => {
      const tabId = typeof message.tabId === "number" ? message.tabId : null;
      if (tabId == null) {
        return { ok: false, message: "当前没有可操作的标签页" };
      }
      try {
        const infoMessage = await featureBridge.toggleFeature(message.feature as AdvancedFeatureKey, tabId);
        return { ok: true, message: infoMessage };
      } catch (error) {
        return {
          ok: false,
          message: error instanceof Error ? error.message : "功能切换失败",
        };
      }
    })());
  }

  if (message.type === "popup_set_media_index") {
    return reply(sendResponse, (async () => {
      await mediaBridge.setMediaIndex(Number(message.tabId ?? 0), Number(message.index ?? -1));
      return buildPopupState({
        currentView: "advanced",
      });
    })());
  }

});

void initialize();
