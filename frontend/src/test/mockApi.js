// A complete API object whose answers are the REAL backend responses captured in fixtures/api (scripts/capture_frontend_fixtures.py).
import { vi } from "vitest";
import approaches from "./fixtures/api/approaches.json";
import scenarios from "./fixtures/api/scenarios.json";
import capabilities from "./fixtures/api/capabilities.json";
import topology from "./fixtures/api/topology.json";
import models from "./fixtures/api/models.json";
import modelEval from "./fixtures/api/model_evaluation.json";
import list from "./fixtures/api/experiments_list.json";
import expMbb from "./fixtures/api/exp_mbb.json";
import expBasic from "./fixtures/api/exp_basic.json";
import mMbb from "./fixtures/api/exp_mbb_metrics.json";
import mBasic from "./fixtures/api/exp_basic_metrics.json";
import hMbb from "./fixtures/api/exp_mbb_handoffs.json";
import eMbb from "./fixtures/api/exp_mbb_events.json";
import pMbb from "./fixtures/api/exp_mbb_predictions.json";
import trMbb from "./fixtures/api/exp_mbb_transitions.json";

export const fx = { approaches, scenarios, capabilities, topology, models, modelEval, list, expMbb, expBasic, mMbb, mBasic, hMbb, eMbb, pMbb, trMbb };
const byId = { [expMbb.id]: { exp: expMbb, metrics: mMbb }, [expBasic.id]: { exp: expBasic, metrics: mBasic } };

export function makeApi(over = {}) {
  return {
    health: vi.fn(async () => ({ status: "ok" })),
    approaches: vi.fn(async () => approaches), scenarios: vi.fn(async () => scenarios), capabilities: vi.fn(async () => capabilities), topology: vi.fn(async () => topology),
    models: vi.fn(async () => models), modelEvaluation: vi.fn(async () => modelEval), status: vi.fn(async () => ({})),
    validateExperiment: vi.fn(async () => ({ valid: true, issues: [], warnings: [], resolved: {} })),
    startExperiment: vi.fn(async () => ({ data: { ...expMbb, status: "queued" }, status: 202, replayed: false })),
    cancelExperiment: vi.fn(async () => ({ data: { ...expMbb, status: "cancelled" }, status: 200, replayed: false })),
    listExperiments: vi.fn(async (p = {}) => { const statuses = [].concat(p.status || []); const items = list.items.filter((e) => !statuses.length || statuses.includes(e.status)); return { ...list, items, total: items.length }; }),
    getExperiment: vi.fn(async (id) => (byId[id] || byId[expMbb.id]).exp),
    transitions: vi.fn(async () => trMbb),
    metrics: vi.fn(async (id) => (byId[id] || byId[expMbb.id]).metrics),
    handoffs: vi.fn(async () => hMbb), events: vi.fn(async () => eMbb), predictions: vi.fn(async () => pMbb),
    exportExperiment: vi.fn(async (id, o) => ({ blob: { size: 1 }, filename: `${id}.${o.format}`, contentType: "x" })),
    exportHistory: vi.fn(async (f, fmt) => ({ blob: { size: 1 }, filename: `history.${fmt}`, contentType: "x" })),
    ...over,
  };
}
