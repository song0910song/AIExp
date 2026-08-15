"use client";

import { useEffect, useRef, useState } from "react";
import { Download, ExternalLink, FolderArchive, RotateCcw, Search, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Luminaire, PhotometryAsset, Project } from "@/lib/types";
import { PhotometryControls } from "./LuminairePhotometryControls";
import { BusyButton, EmptyState, Field, Notice, Panel, StatusPill, formatNumber, toNullableNumber } from "./ui";

export function LuminairePanel({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [assets, setAssets] = useState<Record<string, PhotometryAsset>>({});
  const [assetBusy, setAssetBusy] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<{ completed: number; total: number; failed: number } | null>(null);
  const requestInFlight = useRef(false);

  useEffect(() => {
    let active = true;
    setAssets({});
    void api.photometryAssets(project.project_id)
      .then((response) => {
        if (!active) return;
        setAssets(Object.fromEntries(response.assets.map((asset) => [asset.luminaire_id, asset])));
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, [project.project_id, project.revision]);

  function setAsset(asset: PhotometryAsset) {
    setAssets((current) => ({ ...current, [asset.luminaire_id]: asset }));
  }

  async function downloadPhotometry(item: Luminaire) {
    if (assetBusy) return;
    setAssetBusy(item.luminaire_id);
    setNotice(null);
    try {
      const response = await api.downloadLuminairePhotometry(project.project_id, item.luminaire_id);
      setAsset(response.asset);
      setNotice({
        tone: response.asset.status === "downloaded" ? "success" : "danger",
        text: response.asset.status === "downloaded"
          ? `已保存 ${item.article_name} 的配光 ZIP 和解压文件。`
          : response.asset.error ?? `${item.article_name} 的配光 ZIP 下载失败。`,
      });
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "配光 ZIP 下载失败" });
    } finally {
      setAssetBusy(null);
    }
  }

  async function downloadBatch(retryFailures: boolean) {
    if (assetBusy) return;
    const candidates = project.luminaires.filter((item) => {
      const status = assets[item.luminaire_id]?.status;
      return item.has_photometry_download && (retryFailures ? status === "failed" : status !== "downloaded");
    });
    if (!candidates.length) {
      setNotice({ tone: "success", text: retryFailures ? "没有需要重试的失败下载。" : "所有可用配光 ZIP 均已保存。" });
      return;
    }
    setAssetBusy("batch");
    setNotice(null);
    let failed = 0;
    setDownloadProgress({ completed: 0, total: candidates.length, failed: 0 });
    for (const [index, item] of candidates.entries()) {
      try {
        const response = await api.downloadLuminairePhotometry(project.project_id, item.luminaire_id);
        setAsset(response.asset);
        if (response.asset.status !== "downloaded") failed += 1;
      } catch {
        failed += 1;
      }
      setDownloadProgress({ completed: index + 1, total: candidates.length, failed });
    }
    setNotice({
      tone: failed ? "danger" : "success",
      text: failed ? `配光下载完成，${failed} 款失败，可使用“重试失败项”。` : `已保存 ${candidates.length} 款灯具的配光资产。`,
    });
    setAssetBusy(null);
  }

  async function removeLuminaire(item: Luminaire) {
    if (assetBusy || !window.confirm(`移除候选灯具“${item.article_name}”及其已保存的配光资产？`)) return;
    setAssetBusy(item.luminaire_id);
    setNotice(null);
    try {
      const updated = await api.removeLuminaire(project.project_id, item.luminaire_id, project.revision);
      onProject(updated);
      setAssets((current) => {
        const next = { ...current };
        delete next[item.luminaire_id];
        return next;
      });
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "移除候选灯具失败" });
    } finally {
      setAssetBusy(null);
    }
  }

  async function search(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // State updates are asynchronous, so `busy` alone cannot prevent two
    // rapid submit events from issuing duplicate save requests.
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.searchLuminaires(project.project_id, {
        keyword: String(data.get("keyword")),
        language: "zh",
        brand: String(data.get("brand")) || null,
        target_illuminance_lx: toNullableNumber(String(data.get("target_illuminance_lx"))),
        target_cct_k: toNullableNumber(String(data.get("target_cct_k"))),
        min_cri: toNullableNumber(String(data.get("min_cri"))),
        max_ugr: toNullableNumber(String(data.get("max_ugr"))),
        max_power_w: toNullableNumber(String(data.get("max_power_w"))),
        min_ip_rating: String(data.get("min_ip_rating")) || null,
        max_results: Number(data.get("max_results")),
        expected_revision: project.revision,
        save_to_project: true,
      });
      if (result.project) onProject(result.project);
      const savedCount = result.saved_count ?? result.candidates.length;
      const savedMessage = savedCount
        ? `已从 DIALux 保存 ${savedCount} 款候选灯具`
        : "候选灯具已在项目清单中，无需重复保存";
      const rebaseMessage = result.rebased ? "；已自动合并其他位置刚保存的项目修改" : "";
      setNotice({ tone: "success", text: `${savedMessage}${rebaseMessage}；仍需在 DIALux evo 中完成项目核验。` });
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "DIALux 查询失败" });
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="content-stack">
      <div className="section-heading"><div><p className="eyebrow">DIALUX LUMINAIRE FINDER</p><h1>真实灯具候选</h1><p>产品目录用于初筛；项目照度、均匀度和 UGR 必须通过 DIALux evo 核验。</p></div></div>
      <Panel title="选灯条件" eyebrow="SERVER-SIDE FILTER">
        <form onSubmit={search} className="form-grid form-grid-four">
          <Field label="全文关键词" wide hint="支持中文、英文品类词和型号；例如：嵌入式筒灯、recessed LED downlight。"><input name="keyword" required defaultValue="嵌入式筒灯" /></Field>
          <Field label="目标照度 / lx"><input name="target_illuminance_lx" type="number" min="1" step="10" defaultValue={project.brief.target_illuminance_lx ?? ""} placeholder="300" /></Field>
          <Field label="目标色温 / K"><input name="target_cct_k" type="number" min="1000" step="100" defaultValue={project.brief.target_cct_k ?? ""} /></Field>
          <Field label="最低显色指数 / Ra"><input name="min_cri" type="number" min="0" max="100" defaultValue={project.brief.min_cri ?? ""} /></Field>
          <Field label="目标 UGR（不高于）"><input name="max_ugr" type="number" min="10" max="40" step="1" defaultValue={project.brief.target_ugr ?? ""} placeholder="19" /></Field>
          <details className="field-wide luminaire-more-conditions">
            <summary>更多条件（用户明确说明后填写）</summary>
            <div className="form-grid form-grid-four">
              <Field label="品牌"><input name="brand" placeholder="可选" /></Field>
              <Field label="最大功率 / W"><input name="max_power_w" type="number" min="0.1" step="0.1" placeholder="25" /></Field>
              <Field label="最低 IP 等级"><input name="min_ip_rating" placeholder="IP20" /></Field>
              <Field label="返回数量"><select name="max_results" defaultValue="5"><option value="1">1 款</option><option value="3">3 款</option><option value="5">5 款</option></select></Field>
            </div>
          </details>
          <div className="field-wide"><BusyButton busy={busy} type="submit"><Search size={16} />查询并保存候选</BusyButton></div>
        </form>
        {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}
      </Panel>

      <Panel title="项目候选清单" eyebrow={`${project.luminaires.length} SAVED CANDIDATES`}>
        {project.luminaires.length ? (
          <div className="luminaire-grid">
            <div className="photometry-batch-actions">
              <div>
                <strong>配光资产</strong>
                <span>{downloadProgress ? `下载进度 ${downloadProgress.completed}/${downloadProgress.total}，失败 ${downloadProgress.failed}` : "下载后会保存 ZIP、IES/LDT/ULD 文件及哈希清单。"}</span>
              </div>
              <div>
                <BusyButton busy={assetBusy === "batch"} disabled={Boolean(assetBusy)} type="button" onClick={() => void downloadBatch(false)}><Download size={16} />下载全部配光</BusyButton>
                <button className="button button-quiet" type="button" disabled={Boolean(assetBusy)} onClick={() => void downloadBatch(true)}><RotateCcw size={16} />重试失败项</button>
              </div>
            </div>
            {project.luminaires.toReversed().map((item, index) => (
              <article className="luminaire-card" key={`${item.luminaire_id}-${index}`}>
                <PhotometryControls
                  item={item}
                  asset={assets[item.luminaire_id]}
                  busy={assetBusy === item.luminaire_id || assetBusy === "batch"}
                  downloadUrl={api.savedLuminairePhotometryUrl(project.project_id, item.luminaire_id)}
                  onDownload={() => void downloadPhotometry(item)}
                  onRemove={() => void removeLuminaire(item)}
                />
                <div className="luminaire-image">
                  {item.image_url ? <img src={item.image_url} alt={`${item.brand_name ?? ""} ${item.article_name}`} /> : <span>产品图未提供</span>}
                  <StatusPill status={item.matching_status === "matches" ? "success" : "warning"}>{item.matching_status}</StatusPill>
                </div>
                <div className="luminaire-body">
                  <p className="eyebrow">{item.brand_name ?? "品牌未提供"}</p>
                  <h3>{item.article_name}</h3>
                  <p className="luminaire-summary">{item.summary ?? item.technical_summary ?? "技术摘要未提供"}</p>
                  <dl className="spec-row"><div><dt>功率</dt><dd>{formatNumber(item.power_w)} W</dd></div><div><dt>CCT</dt><dd>{formatNumber(item.cct_k, 0)} K</dd></div><div><dt>CRI</dt><dd>{formatNumber(item.cri, 0)}</dd></div><div><dt>UGR</dt><dd>{formatNumber(item.ugr, 0)}</dd></div><div><dt>防护</dt><dd>{item.ip_rating ?? "—"}</dd></div></dl>
                  <div className="resource-row"><span className={item.has_uld ? "available" : ""}>ULD {item.has_uld ? "可用" : "缺失"}</span><span className={item.has_photometry_download ? "available" : ""}>配光 {item.has_photometry_download ? "可用" : "缺失"}</span></div>
                  {item.missing_requested_fields.length ? <p className="missing-fields">缺少筛选字段：{item.missing_requested_fields.join("、")}</p> : null}
                  <a className="text-link" href={item.detail_url} target="_blank" rel="noreferrer">查看 DIALux 详情 <ExternalLink size={14} /></a>
                </div>
              </article>
            ))}
          </div>
        ) : <EmptyState title="尚无候选灯具">先确认任务书和规范条件，再从 DIALux Luminaire Finder 查询真实产品。</EmptyState>}
      </Panel>
    </div>
  );
}
