"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Calculator, RefreshCw, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import type {
  PhotometryAsset,
  PhotometryPreviewPayload,
  Project,
} from "@/lib/types";
import { BusyButton, EmptyState, formatNumber, Notice } from "./ui";

type FormState = {
  luminaireId: string;
  groupId: string;
  fixtureRows: number;
  fixtureColumns: number;
  mountingHeight: string;
  utilizationFactor: string;
  maintenanceFactor: string;
  totalFlux: string;
};

function cellColor(value: number, min: number, max: number): string {
  const span = max - min;
  const t = span > 0 ? (value - min) / span : 0;
  return `hsl(${Math.round(228 - 183 * t)}, ${Math.round(70 + 20 * t)}%, ${Math.round(90 - 32 * t)}%)`;
}

export function PhotometryPreviewCard({ project }: { project: Project }) {
  const [assets, setAssets] = useState<Record<string, PhotometryAsset>>({});
  const [form, setForm] = useState<FormState | null>(null);
  const [preview, setPreview] = useState<PhotometryPreviewPayload | null>(null);
  const [staleReasons, setStaleReasons] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const eligibleIds = useMemo(() => {
    const byId = new Map(project.luminaires.map((item) => [item.luminaire_id, item]));
    return project.selected_luminaire_ids.filter((id) => {
      const asset = assets[id];
      const luminaire = byId.get(id);
      return (
        luminaire !== undefined &&
        asset?.status === "downloaded" &&
        (asset.extracted_files ?? []).some((file) => file.file_type === "ies" || file.file_type === "ldt")
      );
    });
  }, [project.luminaires, project.selected_luminaire_ids, assets]);

  useEffect(() => {
    let active = true;
    void api.photometryAssets(project.project_id).then((response) => {
      if (!active) return;
      setAssets(Object.fromEntries(response.assets.map((item) => [item.luminaire_id, item])));
    }).catch(() => undefined);
    void api
      .getPhotometryPreview(project.project_id)
      .then((stored) => {
        if (!active) return;
        setPreview(stored.preview);
        setStaleReasons(stored.stale_reasons);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [project.project_id]);

  // Seed the form defaults from the current brief/group once luminaire options exist.
  useEffect(() => {
    if (form || eligibleIds.length === 0) return;
    const group =
      project.brief.lighting_groups.find((item) =>
        project.luminaire_group_assignments[item.group_id]?.includes(eligibleIds[0]),
      ) ?? project.brief.lighting_groups[0];
    const luminaire = project.luminaires.find((item) => item.luminaire_id === eligibleIds[0]);
    setForm({
      luminaireId: eligibleIds[0],
      groupId: group?.group_id ?? "",
      fixtureRows: 2,
      fixtureColumns: 3,
      mountingHeight: group ? String(group.mounting_height_m) : "",
      utilizationFactor: group?.utilization_factor ? String(group.utilization_factor) : "",
      maintenanceFactor: group?.maintenance_factor ? String(group.maintenance_factor) : "",
      totalFlux: luminaire?.luminous_flux_lm ? String(luminaire.luminous_flux_lm) : "",
    });
  }, [eligibleIds, form, project.brief.lighting_groups, project.luminaire_group_assignments, project.luminaires]);

  const refreshStored = useCallback(async () => {
    try {
      const stored = await api.getPhotometryPreview(project.project_id);
      setPreview(stored.preview);
      setStaleReasons(stored.stale_reasons);
    } catch {
      /* no stored preview yet */
    }
  }, [project.project_id]);

  async function runPreview() {
    if (!form) return;
    setBusy(true);
    setError(null);
    try {
      const response = await api.createPhotometryPreview(project.project_id, {
        expected_revision: project.revision,
        luminaire_id: form.luminaireId,
        lighting_group_id: form.groupId || null,
        fixture_rows: Number(form.fixtureRows),
        fixture_columns: Number(form.fixtureColumns),
        mounting_height_m: form.mountingHeight ? Number(form.mountingHeight) : null,
        utilization_factor: form.utilizationFactor ? Number(form.utilizationFactor) : null,
        maintenance_factor: form.maintenanceFactor ? Number(form.maintenanceFactor) : null,
        total_flux_lm: form.totalFlux ? Number(form.totalFlux) : null,
      });
      setPreview(response.preview);
      setStaleReasons([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "照度预览生成失败");
    } finally {
      setBusy(false);
    }
  }

  if (project.selected_luminaire_ids.length === 0) {
    return (
      <section className="overview-section photometry-preview">
        <header>
          <div>
            <p className="eyebrow">ILLUMINANCE PREVIEW</p>
            <h2>工作面照度预览（近似）</h2>
          </div>
          <Calculator size={18} />
        </header>
        <EmptyState title="尚无最终选定灯具">确认最终灯具并下载配光文件后，可在此生成近似照度预览。</EmptyState>
      </section>
    );
  }

  if (eligibleIds.length === 0) {
    return (
      <section className="overview-section photometry-preview">
        <header>
          <div>
            <p className="eyebrow">ILLUMINANCE PREVIEW</p>
            <h2>工作面照度预览（近似）</h2>
          </div>
          <Calculator size={18} />
        </header>
        <EmptyState title="最终选定灯具还没有 IES/LDT 配光文件">
          在上方为最终选定灯具下载配光数据后，即可基于真实配光生成趋势预览。
        </EmptyState>
      </section>
    );
  }

  const result = preview?.result;

  return (
    <section className="overview-section photometry-preview">
      <header>
        <div>
          <p className="eyebrow">ILLUMINANCE PREVIEW</p>
          <h2>工作面照度预览（近似）</h2>
        </div>
        <Calculator size={18} />
      </header>

      {preview && staleReasons.length > 0 ? (
        <Notice tone="warning">
          <strong>已保存的预览可能过期：</strong> {staleReasons.join("；")}。请重新生成。
        </Notice>
      ) : null}
      {error ? <Notice tone="danger">{error}</Notice> : null}

      {form ? (
        <div className="photometry-preview-form">
          <label>
            灯具
            <select
              value={form.luminaireId}
              onChange={(event) => setForm({ ...form, luminaireId: event.target.value })}
            >
              {eligibleIds.map((id) => (
                <option key={id} value={id}>
                  {project.luminaires.find((item) => item.luminaire_id === id)?.article_name ?? id}
                </option>
              ))}
            </select>
          </label>
          <label>
            布灯（行 × 列）
            <span className="pair">
              <input
                type="number"
                min={1}
                max={24}
                value={form.fixtureRows}
                onChange={(event) => setForm({ ...form, fixtureRows: Math.max(1, Math.min(24, Number(event.target.value) || 1)) })}
              />
              <input
                type="number"
                min={1}
                max={24}
                value={form.fixtureColumns}
                onChange={(event) => setForm({ ...form, fixtureColumns: Math.max(1, Math.min(24, Number(event.target.value) || 1)) })}
              />
            </span>
          </label>
          <label>
            吊装点高度 m
            <input
              type="number"
              step="0.05"
              min={0.8}
              value={form.mountingHeight}
              onChange={(event) => setForm({ ...form, mountingHeight: event.target.value })}
            />
          </label>
          <label>
            利用系数 UF
            <input
              type="number"
              step="0.01"
              min={0.01}
              max={1}
              placeholder="留空＝仅直射"
              value={form.utilizationFactor}
              onChange={(event) => setForm({ ...form, utilizationFactor: event.target.value })}
            />
          </label>
          <label>
            维护系数 MF
            <input
              type="number"
              step="0.01"
              min={0.01}
              max={1}
              value={form.maintenanceFactor}
              onChange={(event) => setForm({ ...form, maintenanceFactor: event.target.value })}
            />
          </label>
          <label>
            单灯光通量 lm
            <input
              type="number"
              min={1}
              placeholder="来自候选数据"
              value={form.totalFlux}
              onChange={(event) => setForm({ ...form, totalFlux: event.target.value })}
            />
          </label>
          <BusyButton
            className="button button-primary run-preview"
            busy={busy}
            onClick={() => void runPreview()}
          >
            <RefreshCw size={15} />生成预览
          </BusyButton>
        </div>
      ) : null}

      {result ? (
        <>
          <div className="photometry-preview-stats">
            <article><span>平均照度</span><strong>{formatNumber(result.average_illuminance_lx)}<small> lx</small></strong></article>
            <article><span>最小 / 最大</span><strong>{formatNumber(result.minimum_illuminance_lx, 0)} / {formatNumber(result.maximum_illuminance_lx, 0)}</strong></article>
            <article><span>均匀度 U0</span><strong>{formatNumber(result.uniformity_u0, 3)}</strong></article>
            <article><span>装机光通量</span><strong>{formatNumber(result.installed_flux_lm, 0)}<small> lm</small></strong></article>
          </div>

          <div
            className="preview-heatmap"
            style={{ gridTemplateColumns: `repeat(${result.grid_columns}, 1fr)` }}
            role="img"
            aria-label={`工作面照度分布热力图，平均 ${result.average_illuminance_lx} 勒克斯`}
          >
            {result.illuminance_lx.flatMap((row, rowIndex) =>
              row.map((value, columnIndex) => (
                <span
                  key={`${rowIndex}-${columnIndex}`}
                  title={`y${rowIndex} · x${columnIndex}: ${value} lx`}
                  style={{
                    backgroundColor: cellColor(value, result.minimum_illuminance_lx, result.maximum_illuminance_lx),
                  }}
                  className={rowIndex === 0 && columnIndex === 0 ? "corner-marker" : ""}
                >
                  {result.grid_columns <= 14 ? Math.round(value) : ""}
                </span>
              )),
            )}
          </div>
          <p className="heatmap-scale-note">
            颜色越暖照度越高；网格 {result.grid_rows} 行 × {result.grid_columns} 列，工作面取样点约 {result.grid_rows * result.grid_columns} 个。
          </p>

          {preview?.warnings.length ? (
            <Notice tone="warning"><TriangleAlert size={15} />{preview.warnings.join(" ")}</Notice>
          ) : null}
          <details className="preview-limitations">
            <summary>假设与限制（预览为近似结果）</summary>
            <ul>
              {[...result.assumptions, ...result.limitations].map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
            <p>
              求解器版本：{result.solver_version}；输入快照：
              <code>{preview?.input_snapshot_sha256.slice(0, 16)}…</code>
              {preview ? `；来源配光：${preview.luminaire.source_file}` : null}
            </p>
          </details>
        </>
      ) : (
        <EmptyState title="尚未生成预览">选择布灯参数后点击“生成预览”；结果仅供参考，合规结论仍需 DIALux evo 核验。</EmptyState>
      )}
    </section>
  );
}
