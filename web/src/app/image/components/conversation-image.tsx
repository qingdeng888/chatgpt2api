"use client";

import { useEffect, useState, type SyntheticEvent } from "react";
import { ImageOff } from "lucide-react";

import { cn } from "@/lib/utils";

type ConversationImageProps = {
  src: string;
  alt?: string;
  className?: string;
  onLoad?: (event: SyntheticEvent<HTMLImageElement>) => void;
};

/**
 * 对话历史里的图片：可能被图片保留期清理（config.cleanup_old_images）后 src 仍指向 404。
 * 加载失败时优雅降级为占位，而不是留下浏览器的破图图标 —— 对话文本与提示词不受影响。
 */
export function ConversationImage({ src, alt = "", className, onLoad }: ConversationImageProps) {
  const [failed, setFailed] = useState(false);

  // src 变化（例如从 loading 切到结果）时重新尝试加载。
  useEffect(() => {
    setFailed(false);
  }, [src]);

  if (!src || failed) {
    return (
      <div
        className={cn(
          className,
          "flex flex-col items-center justify-center gap-1 bg-stone-100 p-2 text-center text-stone-400",
        )}
        role="img"
        aria-label={alt || "图片已过期"}
      >
        <ImageOff className="size-4 shrink-0" />
        <span className="text-[10px] leading-tight">图片已过期（超过保留期）</span>
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      className={className}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      onLoad={onLoad}
    />
  );
}
