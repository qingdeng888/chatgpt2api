"use client";

import localforage from "localforage";

import type { ImageModel } from "@/lib/api";
import { httpRequest } from "@/lib/request";

export type ImageConversationMode = "generate" | "edit";

export type StoredReferenceImage = {
  name: string;
  type: string;
  // 内存态 / 迁移态：新上传或从 IndexedDB 读出的参考图带 base64，仅用于本地预览。
  dataUrl?: string;
  // 持久化态：参考图落盘后服务器返回的裸相对路径，JSON 里只存这个。
  rel?: string;
  // 展示态：服务器读取时按当前请求把 rel 重算出的可访问地址。
  url?: string;
};

export type StoredImage = {
  id: string;
  taskId?: string;
  status?: "loading" | "success" | "error";
  b64_json?: string;
  url?: string;
  // 持久化态：生成结果图的裸相对路径，与 url 二选一，url 由服务器重算。
  rel?: string;
  revised_prompt?: string;
  error?: string;
};

export type ImageTurnStatus = "queued" | "generating" | "success" | "error";

export type ImageTurn = {
  id: string;
  prompt: string;
  model: ImageModel;
  mode: ImageConversationMode;
  referenceImages: StoredReferenceImage[];
  count: number;
  size: string;
  ratio: string;
  tier: string;
  quality: string;
  images: StoredImage[];
  createdAt: string;
  status: ImageTurnStatus;
  error?: string;
  promptDeleted?: boolean;
  resultsDeleted?: boolean;
};

export type ImageConversation = {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  turns: ImageTurn[];
};

export type ImageConversationStats = {
  queued: number;
  running: number;
};

// 仅用于「首次登录自动迁移」：把浏览器 IndexedDB 里的旧历史读出来上传，之后清空。
// 正常的读写一律走服务器 API（见下方 list/save/... 函数）。
const imageConversationStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "image_conversations",
});

const IMAGE_CONVERSATIONS_KEY = "items";

/**
 * 每个对话最多保留的参考图张数，与服务端 image_conversation_service.MAX_REFERENCE_IMAGES 对齐。
 * 服务端超出后从最旧的 turn 开始丢；前端提前截断，避免把注定被丢弃的图上传一遍。
 */
export const MAX_REFERENCE_IMAGES = 30;
let imageConversationWriteQueue: Promise<void> = Promise.resolve();

// 参考图上传结果缓存：同一 dataUrl 在一次会话里只上传一次，避免重复落盘。
const referenceUploadCache = new Map<string, StoredReferenceImage>();

function normalizeStoredImage(image: StoredImage): StoredImage {
  const normalized = {
    ...image,
    taskId: typeof image.taskId === "string" && image.taskId ? image.taskId : undefined,
    url: typeof image.url === "string" && image.url ? image.url : undefined,
    rel: typeof image.rel === "string" && image.rel ? image.rel : undefined,
    revised_prompt: typeof image.revised_prompt === "string" ? image.revised_prompt : undefined,
  };
  if (image.status === "loading" || image.status === "error" || image.status === "success") {
    return normalized;
  }
  return {
    ...normalized,
    status: image.b64_json || image.url || image.rel ? "success" : "loading",
  };
}

function normalizeReferenceImage(image: StoredReferenceImage): StoredReferenceImage {
  const normalized: StoredReferenceImage = {
    name: image.name || "reference.png",
    type: image.type || "image/png",
  };
  // 三种形态都可能存在：dataUrl（内存/迁移）、rel+url（服务器持久化）。一律保留，
  // 由消费方按优先级取用，避免把服务器返回的 rel/url 参考图误删。
  if (typeof image.dataUrl === "string" && image.dataUrl) {
    normalized.dataUrl = image.dataUrl;
  }
  if (typeof image.rel === "string" && image.rel) {
    normalized.rel = image.rel;
  }
  if (typeof image.url === "string" && image.url) {
    normalized.url = image.url;
  }
  return normalized;
}

function hasReferencePayload(image: StoredReferenceImage): boolean {
  return Boolean(
    (typeof image.dataUrl === "string" && image.dataUrl) ||
      (typeof image.rel === "string" && image.rel) ||
      (typeof image.url === "string" && image.url),
  );
}

function dataUrlMimeType(dataUrl: string) {
  const match = dataUrl.match(/^data:(.*?);base64,/);
  return match?.[1] || "image/png";
}

function getLegacyReferenceImages(source: Record<string, unknown>): StoredReferenceImage[] {
  if (Array.isArray(source.referenceImages)) {
    return source.referenceImages
      .filter((image): image is StoredReferenceImage => {
        if (!image || typeof image !== "object") {
          return false;
        }
        return hasReferencePayload(image as StoredReferenceImage);
      })
      .map(normalizeReferenceImage);
  }

  if (source.sourceImage && typeof source.sourceImage === "object") {
    const image = source.sourceImage as { dataUrl?: unknown; fileName?: unknown };
    if (typeof image.dataUrl === "string" && image.dataUrl) {
      return [
        {
          name: typeof image.fileName === "string" && image.fileName ? image.fileName : "reference.png",
          type: dataUrlMimeType(image.dataUrl),
          dataUrl: image.dataUrl,
        },
      ];
    }
  }

  return [];
}

function normalizeTurn(turn: ImageTurn & Record<string, unknown>): ImageTurn {
  const normalizedImages = Array.isArray(turn.images) ? turn.images.map(normalizeStoredImage) : [];
  const derivedStatus: ImageTurnStatus =
    normalizedImages.some((image) => image.status === "loading")
      ? "generating"
      : normalizedImages.some((image) => image.status === "error")
        ? "error"
        : "success";

  return {
    id: String(turn.id || `${Date.now()}`),
    prompt: String(turn.prompt || ""),
    model: (turn.model as ImageModel) || "gpt-image-2",
    mode: turn.mode === "edit" ? "edit" : "generate",
    referenceImages: getLegacyReferenceImages(turn),
    count: Math.max(1, Number(turn.count || normalizedImages.length || 1)),
    size: typeof turn.size === "string" ? turn.size : "",
    ratio: typeof turn.ratio === "string" && turn.ratio ? turn.ratio : "1:1",
    tier: typeof turn.tier === "string" && turn.tier ? turn.tier : "1k",
    quality: typeof turn.quality === "string" && turn.quality ? turn.quality : "auto",
    images: normalizedImages,
    createdAt: String(turn.createdAt || new Date().toISOString()),
    status:
      turn.status === "queued" ||
      turn.status === "generating" ||
      turn.status === "success" ||
      turn.status === "error"
        ? turn.status
        : derivedStatus,
    error: typeof turn.error === "string" ? turn.error : undefined,
    promptDeleted: turn.promptDeleted === true,
    resultsDeleted: turn.resultsDeleted === true,
  };
}

function normalizeConversation(conversation: ImageConversation & Record<string, unknown>): ImageConversation {
  const turns = Array.isArray(conversation.turns)
    ? conversation.turns.map((turn) => normalizeTurn(turn as ImageTurn & Record<string, unknown>))
    : [
        normalizeTurn({
          id: String(conversation.id || `${Date.now()}`),
          prompt: String(conversation.prompt || ""),
          model: (conversation.model as ImageModel) || "gpt-image-2",
          mode: conversation.mode === "edit" ? "edit" : "generate",
          referenceImages: getLegacyReferenceImages(conversation),
          count: Number(conversation.count || 1),
          size: typeof conversation.size === "string" ? conversation.size : "",
          ratio: typeof conversation.ratio === "string" && conversation.ratio ? conversation.ratio : "1:1",
          tier: typeof conversation.tier === "string" && conversation.tier ? conversation.tier : "1k",
          quality: typeof conversation.quality === "string" && conversation.quality ? conversation.quality : "auto",
          images: Array.isArray(conversation.images) ? (conversation.images as StoredImage[]) : [],
          createdAt: String(conversation.createdAt || new Date().toISOString()),
          status:
            conversation.status === "generating" || conversation.status === "success" || conversation.status === "error"
              ? conversation.status
              : "success",
          error: typeof conversation.error === "string" ? conversation.error : undefined,
        }),
      ];
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;

  return {
    id: String(conversation.id || `${Date.now()}`),
    title: String(conversation.title || ""),
    createdAt: String(conversation.createdAt || lastTurn?.createdAt || new Date().toISOString()),
    updatedAt: String(conversation.updatedAt || lastTurn?.createdAt || new Date().toISOString()),
    turns,
  };
}

function sortImageConversations(conversations: ImageConversation[]): ImageConversation[] {
  return [...conversations].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

function queueImageConversationWrite<T>(operation: () => Promise<T>): Promise<T> {
  const result = imageConversationWriteQueue.then(operation);
  imageConversationWriteQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

function dataUrlToBlob(dataUrl: string): Blob {
  const [header, content] = dataUrl.split(",", 2);
  const mimeType = header.match(/data:(.*?);base64/)?.[1] || "image/png";
  const binary = atob(content || "");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mimeType });
}

/**
 * 保存前把「只有 dataUrl 还没有 rel」的参考图上传到服务器换取相对路径。
 * 服务器只持久化 rel（30 张 base64 约 88MB，落盘后只需约 1KB），所以必须先上传。
 * 上传成功的参考图回填 rel/url 并去掉 dataUrl；失败的保留原样，下次保存重试。
 */
async function ensureReferenceImagesUploaded(conversation: ImageConversation): Promise<void> {
  const pending = new Map<string, StoredReferenceImage>();
  for (const turn of conversation.turns) {
    for (const reference of turn.referenceImages) {
      if (reference.rel || !reference.dataUrl || referenceUploadCache.has(reference.dataUrl)) {
        continue;
      }
      pending.set(reference.dataUrl, reference);
    }
  }

  if (pending.size > 0) {
    const formData = new FormData();
    const dataUrls = [...pending.keys()];
    dataUrls.forEach((dataUrl, index) => {
      const reference = pending.get(dataUrl)!;
      formData.append("image", dataUrlToBlob(dataUrl), reference.name || `reference-${index + 1}.png`);
    });
    const { items } = await httpRequest<{ items: Array<{ rel: string; url: string }> }>(
      "/api/image-references",
      { method: "POST", body: formData },
    );
    dataUrls.forEach((dataUrl, index) => {
      const uploaded = items[index];
      const source = pending.get(dataUrl);
      if (uploaded?.rel && source) {
        referenceUploadCache.set(dataUrl, {
          name: source.name,
          type: source.type,
          rel: uploaded.rel,
          url: uploaded.url,
        });
      }
    });
  }

  for (const turn of conversation.turns) {
    turn.referenceImages = turn.referenceImages.map((reference) => {
      if (reference.rel) {
        // 已是持久化形态：去掉 dataUrl，避免把 base64 再次发给服务器。
        const { dataUrl: _dataUrl, ...rest } = reference;
        return rest;
      }
      const cached = reference.dataUrl ? referenceUploadCache.get(reference.dataUrl) : undefined;
      return cached ? { ...cached } : reference;
    });
  }
}

export async function listImageConversations(): Promise<ImageConversation[]> {
  const { items } = await httpRequest<{ items: Array<ImageConversation & Record<string, unknown>> }>(
    "/api/image-conversations",
  );
  return sortImageConversations((items || []).map(normalizeConversation));
}

export async function saveImageConversations(conversations: ImageConversation[]): Promise<void> {
  await queueImageConversationWrite(async () => {
    const normalized = conversations.map(normalizeConversation);
    for (const conversation of normalized) {
      await ensureReferenceImagesUploaded(conversation);
    }
    await httpRequest("/api/image-conversations/bulk", {
      method: "POST",
      body: { conversations: normalized },
    });
  });
}

export async function saveImageConversation(conversation: ImageConversation): Promise<void> {
  await queueImageConversationWrite(async () => {
    const nextConversation = normalizeConversation(conversation);
    await ensureReferenceImagesUploaded(nextConversation);
    await httpRequest("/api/image-conversations", {
      method: "POST",
      body: { conversation: nextConversation },
    });
  });
}

export async function renameImageConversation(id: string, title: string): Promise<void> {
  await queueImageConversationWrite(async () => {
    await httpRequest(`/api/image-conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { title },
    });
  });
}

export async function deleteImageConversation(id: string): Promise<void> {
  await queueImageConversationWrite(async () => {
    await httpRequest(`/api/image-conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
  });
}

export async function clearImageConversations(): Promise<void> {
  await queueImageConversationWrite(async () => {
    await httpRequest("/api/image-conversations", { method: "DELETE" });
  });
}

/** 读取浏览器 IndexedDB 里的旧历史，仅供首次迁移使用。 */
export async function readLegacyLocalImageConversations(): Promise<ImageConversation[]> {
  const items =
    (await imageConversationStorage.getItem<Array<ImageConversation & Record<string, unknown>>>(
      IMAGE_CONVERSATIONS_KEY,
    )) || [];
  return items.map(normalizeConversation);
}

/** 迁移成功后清空浏览器本地副本，避免下次重复导入。 */
export async function clearLegacyLocalImageConversations(): Promise<void> {
  await imageConversationStorage.removeItem(IMAGE_CONVERSATIONS_KEY);
}

/** 首次迁移：把浏览器本地历史批量上传到服务器（服务器已有的记录以服务器为准）。 */
export async function importLegacyImageConversations(conversations: ImageConversation[]): Promise<number> {
  const normalized = conversations.map(normalizeConversation);
  for (const conversation of normalized) {
    await ensureReferenceImagesUploaded(conversation);
  }
  const response = await httpRequest<{ imported: number }>("/api/image-conversations/import", {
    method: "POST",
    body: { conversations: normalized },
  });
  return response.imported || 0;
}

/**
 * 解析参考图的展示地址：内存态用 dataUrl，持久化态用服务器重算出的 url。
 * 图片被保留期清理后 url 仍在但会 404，那种情况由 <img> 的 onError 兜底占位。
 */
export function getReferenceImageSrc(image: StoredReferenceImage): string {
  return image.dataUrl || image.url || (image.rel ? `/images/${image.rel}` : "");
}

export function getImageConversationStats(conversation: ImageConversation | null): ImageConversationStats {
  if (!conversation) {
    return { queued: 0, running: 0 };
  }

  return conversation.turns.reduce(
    (acc, turn) => {
      if (turn.resultsDeleted) {
        return acc;
      }
      if (turn.status === "queued") {
        acc.queued += 1;
      } else if (turn.status === "generating") {
        acc.running += 1;
      }
      return acc;
    },
    { queued: 0, running: 0 },
  );
}
