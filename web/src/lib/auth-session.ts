"use client";

import { login } from "@/lib/api";
import { clearStoredAuthSession, getStoredAuthSession, setStoredAuthSession, type StoredAuthSession } from "@/store/auth";

export async function getValidatedAuthSession(): Promise<StoredAuthSession | null> {
  const storedSession = await getStoredAuthSession();
  if (!storedSession) {
    return null;
  }

  try {
    const data = await login(storedSession.key);
    const nextSession: StoredAuthSession = {
      key: storedSession.key,
      role: data.role,
      subjectId: data.subject_id,
      name: data.name,
    };
    await setStoredAuthSession(nextSession);
    return nextSession;
  } catch (error) {
    // 仅当服务端明确返回 401（密钥已失效）时才清除会话并登出；
    // 瞬时网络错误（keep-alive 连接复用失败/超时等）不登出，否则一次网络波动就会把用户弹回登录页。
    const err = error as { status?: number; isNetworkError?: boolean };
    if (err.status === 401) {
      await clearStoredAuthSession();
      return null;
    }
    return storedSession;
  }
}
