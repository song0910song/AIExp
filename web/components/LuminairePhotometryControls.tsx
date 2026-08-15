"use client";

import { Download, FolderArchive, RotateCcw, Trash2 } from "lucide-react";
import type { Luminaire, PhotometryAsset } from "@/lib/types";
import { BusyButton, StatusPill } from "./ui";

function assetStatus(asset: PhotometryAsset | undefined): { tone: "success" | "warning" | "danger" | "neutral"; label: string } {
  if (!asset || asset.status === "pending") return { tone: "neutral", label: "待下载" };
  if (asset.status === "downloaded") return { tone: "success", label: "已保存" };
  if (asset.status === "failed") return { tone: "danger", label: "下载失败" };
  return { tone: "warning", label: "无配光 ZIP" };
}

export function PhotometryControls({
  item,
  asset,
  busy,
  downloadUrl,
  onDownload,
  onRemove,
}: {
  item: Luminaire;
  asset: PhotometryAsset | undefined;
  busy: boolean;
  downloadUrl: string;
  onDownload: () => void;
  onRemove: () => void;
}) {
  const status = assetStatus(asset);
  const canDownload = item.has_photometry_download;

  return (
    <div className="photometry-controls">
      <div className="photometry-status">
        <StatusPill status={status.tone}>{status.label}</StatusPill>
        {asset?.downloaded_at ? <small>{new Date(asset.downloaded_at).toLocaleString("zh-CN")}</small> : null}
      </div>
      {asset?.error ? <p className="photometry-error">{asset.error}</p> : null}
      <div className="photometry-control-actions">
        {canDownload ? (
          <BusyButton busy={busy} disabled={busy} type="button" className="button button-secondary" onClick={onDownload}>
            {asset?.status === "failed" ? <RotateCcw size={15} /> : <Download size={15} />}
            {asset?.status === "downloaded" ? "重新下载" : asset?.status === "failed" ? "重试下载" : "下载配光"}
          </BusyButton>
        ) : null}
        {asset?.status === "downloaded" ? <a className="button button-quiet" href={downloadUrl}><Download size={15} />下载 ZIP</a> : null}
        <button className="button button-quiet" type="button" disabled={busy} onClick={onRemove}><Trash2 size={15} />移除</button>
      </div>
      {asset?.extracted_files.length ? (
        <details className="photometry-files">
          <summary><FolderArchive size={15} />查看 ZIP 内配光文件（{asset.extracted_files.length}）</summary>
          <ul>{asset.extracted_files.map((file) => <li key={file.relative_path}><code>{file.relative_path}</code><span>{file.file_type.toUpperCase()} · {file.size_bytes.toLocaleString()} B</span></li>)}</ul>
        </details>
      ) : null}
    </div>
  );
}
