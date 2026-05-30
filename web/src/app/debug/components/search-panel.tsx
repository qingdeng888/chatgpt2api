"use client";

import { useState } from "react";
import { LoaderCircle, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { httpRequest } from "@/lib/request";

import { pretty, type SearchResult } from "./types";

export function SearchPanel() {
  const [prompt, setPrompt] = useState("帮我搜索 chatgpt2api 相关项目");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const runSearch = async () => {
    const value = prompt.trim();
    if (!value) return;
    setLoading(true);
    setError("");
    try {
      setResult(await httpRequest<SearchResult>("/v1/search", { method: "POST", body: { prompt: value } }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid h-full min-h-0 gap-8 lg:grid-cols-[360px_minmax(0,1fr)]">
      <section className="flex min-h-0 flex-col lg:border-r lg:border-stone-200/70 lg:pr-8 dark:lg:border-white/10">
        <div className="flex items-center justify-between gap-3 border-b border-stone-200/70 pb-3 dark:border-white/10">
          <h2 className="text-sm font-medium text-stone-500 dark:text-stone-400">搜索调试</h2>
          <Button size="sm" onClick={() => void runSearch()} disabled={loading || !prompt.trim()}>
            {loading ? <LoaderCircle className="animate-spin" /> : <Search />}
            运行
          </Button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-auto pt-4">
          <div className="space-y-2">
            <Label htmlFor="search-prompt">Prompt</Label>
            <Textarea id="search-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} className="min-h-32 rounded-md border-stone-200/70 bg-transparent shadow-none dark:border-white/10" />
          </div>
          {error ? <div className="rounded-md border border-rose-200 bg-rose-50/60 px-3 py-2 text-sm text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/20 dark:text-rose-300">{error}</div> : null}
          <Textarea value={result ? pretty(result) : "{\n  \"result\": null\n}"} readOnly className="min-h-80 resize-none rounded-md border-stone-200/70 bg-stone-50/50 p-4 font-mono text-xs leading-5 text-stone-600 shadow-none dark:border-white/10 dark:bg-white/[0.03] dark:text-stone-300" />
        </div>
      </section>
      <section className="flex min-h-0 flex-col">
        <div className="border-b border-stone-200/70 pb-3 dark:border-white/10">
          <h2 className="text-sm font-medium text-stone-500 dark:text-stone-400">搜索结果</h2>
        </div>
        <div className="min-h-0 flex-1 overflow-auto pt-4">
          {result ? (
            <div className="space-y-5">
              <div className="flex flex-wrap gap-2">
                <Badge variant="success">{result.status || "done"}</Badge>
                <Badge variant="outline">{result.sources?.length || 0} sources</Badge>
              </div>
              <div className="whitespace-pre-wrap text-sm leading-7 text-stone-700 dark:text-stone-300">{result.answer}</div>
              <div className="grid gap-2 xl:grid-cols-2">
                {(result.sources || []).map((source, index) => (
                  <a key={`${source.url || index}`} href={source.url} target="_blank" rel="noreferrer" className="block rounded-md border border-stone-200/70 p-3 text-sm transition hover:border-stone-300 hover:bg-stone-50/50 dark:border-white/10 dark:hover:bg-white/[0.03]">
                    <div className="line-clamp-2 font-medium">{source.title || source.url || "source"}</div>
                    <div className="mt-1.5 break-all text-xs text-stone-500 dark:text-stone-400">{source.url}</div>
                  </a>
                ))}
              </div>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-stone-400 dark:text-stone-500">暂无搜索结果</div>
          )}
        </div>
      </section>
    </div>
  );
}
