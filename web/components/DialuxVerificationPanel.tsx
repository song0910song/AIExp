"use client";

import { useEffect, useState } from "react";
import { ArrowUpRight, Check, Download, FileImage, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { IlluminanceVerification, Project } from "@/lib/types";
import { BusyButton, Notice, StatusPill, formatNumber } from "./ui";

const methodStatus = {
  pass: "达标",
  fail: "未达标",
  missing: "待补充",
  unverified: "未验证",
  stale: "已过期",
} as const;

function statusTone(status: keyof typeof methodStatus) {
  if (status === "pass") return "success" as const;
  if (status === "fail") return "danger" as const;
  if (status === "missing" || status === "unverified" || status === "stale") return "warning" as const;
  return "neutral" as const;
}

export function DialuxVerificationPanel({
  project,
  onStartAgent,
}: {
  project: Project;
  onStartAgent: () => void;
}) {
  const [verification, setVerification] = useState<IlluminanceVerification | null>(null);
  const [taskUrl, setTaskUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState<"load" | "task" | null>("load");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setBusy("load");
    api.illuminanceVerification(project.project_id)
      .then((result) => { if (active) setVerification(result); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "读取联合检验失败"); })
      .finally(() => { if (active) setBusy(null); });
    return () => { active = false; };
  }, [project.project_id, project.revision]);

  async function createTask() {
    setBusy("task");
    setError(null);
    try {
      const result = await api.generateDeliverable(project.project_id, "dialux-task", project.revision);
      setTaskUrl(result.download_url.replace(/^\/api/, "/backend"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成 DIALux 任务包失败");
    } finally {
      setBusy(null);
    }
  }

  const latestRun = project.simulation_runs.at(-1);
  const checks = verification ? [verification.lumen_method, verification.dialux] : [];

  return (
    <section className="illuminance-verification" aria-label="照度联合检验">
      <header>
        <div><p className="eyebrow">ILLUMINANCE VERIFICATION</p><h2>流明法 + DIALux 联合检验</h2></div>
        {verification ? (
          <StatusPill status={verification.overall_status === "pass" ? "success" : verification.overall_status === "fail" ? "danger" : "warning"}>
            {verification.overall_status === "pass" ? <Check size={13} /> : <RefreshCw size={13} />}
            {verification.overall_status === "pass" ? "已达标" : verification.overall_status === "fail" ? "需修订" : "待验证"}
          </StatusPill>
        ) : null}
      </header>

      {error ? <Notice tone="danger">{error}</Notice> : null}
      {busy === "load" ? <p className="verification-loading"><RefreshCw size={15} className="spin" />正在读取检验状态</p> : null}
      {verification ? (
        <>
          <div className="verification-methods">
            {checks.map((check) => (
              <article key={check.method}>
                <div><span>{check.method === "lumen_method" ? "流明法估算" : "DIALux 仿真"}</span><StatusPill status={statusTone(check.status)}>{methodStatus[check.status]}</StatusPill></div>
                <strong>{formatNumber(check.observed_illuminance_lx, 1)}<small> lx</small></strong>
                <p>{check.explanation}</p>
              </article>
            ))}
            <article className="verification-target">
              <div><span>目标照度</span><StatusPill status="neutral">唯一标准</StatusPill></div>
              <strong>{formatNumber(verification.target_illuminance_lx, 1)}<small> lx</small></strong>
              <p>{verification.difference_percent === null ? "等待两种方法均有结果" : `两种方法结果相差 ${formatNumber(verification.difference_percent, 1)}%`}</p>
            </article>
          </div>
          <div className={`verification-decision verification-decision-${verification.overall_status}`}>
            <div><strong>{verification.message}</strong><small>第 {verification.iteration} 轮 DIALux 结果</small></div>
            {verification.action !== "target_reached" ? <button className="button button-secondary" type="button" onClick={onStartAgent}>继续迭代<ArrowUpRight size={15} /></button> : null}
          </div>
        </>
      ) : null}

      <div className="dialux-evidence-flow">
        <div className="dialux-task-action">
          <div><strong>准备当前版本任务包</strong><p>在 DIALux 中按任务包完成布灯与仿真。</p></div>
          <BusyButton busy={busy === "task"} className="button button-secondary" type="button" onClick={() => void createTask()}><Download size={15} />生成任务包</BusyButton>
          {taskUrl ? <a className="text-link" href={taskUrl}><Download size={14} />下载</a> : null}
        </div>
      </div>

      {latestRun?.vision_analysis ? (
        <div className="dialux-vision-result" aria-label="视觉模型解析结果">
          <div><strong>视觉解析 {formatNumber(latestRun.vision_analysis.maintained_illuminance_lx, 1)} lx</strong><span>置信度 {formatNumber(latestRun.vision_analysis.confidence * 100, 0)}%</span></div>
          <p>{latestRun.vision_analysis.calculation_surface ?? "计算面未标明"} · {latestRun.vision_analysis.metric_label ?? "维持平均照度"}</p>
          {latestRun.metric_source === "manual" ? <small>联合检验采用人工校正值 {formatNumber(latestRun.metrics?.maintained_illuminance_lx, 1)} lx</small> : null}
        </div>
      ) : null}

      {latestRun?.artifacts.length ? (
        <a className="verification-artifact" href={api.dialuxResultArtifactUrl(project.project_id, latestRun.run_id)} target="_blank" rel="noreferrer">
          <FileImage size={15} /><span>查看本轮原始证据：{latestRun.artifacts[0].file_name}</span><ArrowUpRight size={14} />
        </a>
      ) : null}
    </section>
  );
}
