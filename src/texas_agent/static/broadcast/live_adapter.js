/**
 * 转播大屏实时数据适配层 —— 订阅 ws hub, 把总线消息聚合成大屏视图模型。
 *
 * 零依赖, 自动重连。用法(在 DCLogic 组件 componentDidMount 里):
 *
 *   TexasLive.connect({
 *     url: 'ws://<SPARK_IP>:8766',
 *     names: ['陈默','老周','苏晴','大飞'],       // 座位0..n 的显示名(可选)
 *     onUpdate: (vm) => this.setState({ live: vm })  // 每条消息后回调, ≤30/s
 *   });
 *
 * 数据链路: live_cli --ws  →  ws hub(:8765/:8766)  →  本适配层
 * 联调无实体桌时: python tools/replay.py sessions/xxx.jsonl --ws --speed 4
 * 字段语义以 docs/schema.md v0.2 为准; 映射说明见 docs/前端对接-转播大屏.md
 */
(function (global) {
  'use strict';

  const ACT_ZH = { fold: '弃牌', check: '过牌', call: '跟注', raise: '加注到' };
  const STREET_ZH = { preflop: '翻牌前', flop: '翻牌圈', turn: '转牌圈',
                      river: '河牌圈', complete: '摊牌' };

  function freshState() {
    return {
      connected: false,
      handNo: null, blinds: null,
      street: 'preflop', streetZh: '待机',
      board: [],                  // ['Jh','8h','3c'] 点数大写+花色小写
      pot: 0,
      seats: [],                  // [{seat,position,stack,bet,inHand,hole,name}]
      actor: null,                // 轮到谁行动(0基), input_request 驱动
      acts: {},                   // seat → 最近动作文本 '加注到 300'
      equity: {},                 // seat → 胜率% (0~100, analysis_update)
      history: [],                // 胜率之河 [{eq:{seat:pct}, mark:'翻牌'|null}]
      analysis: null,             // 最近一条 analysis_update 全文(进阶指标用)
      gto: {},                    // seat → 最近 gto_hint {node,hero,summary,matrix,actions}
      deviations: [],             // gto_deviation 流水
      audits: [],                 // audit_report 流水(视觉审计, 演示识别存在感)
      alerts: [],
      says: ['等待开局…'],        // 解说流水(commentary)
      prompt: '',                 // 荷官提词(dealer_prompt)
      robot: '待机',              // 顶部/底部状态徽记
      dealSources: {},            // zone → 'vlm'|'ops' (牌面认定来源标记)
      winner: null, payoffs: null,
      complete: false,
      lastSeq: 0,
    };
  }

  const S = { st: freshState(), names: [], cb: null, ws: null, url: null };

  function applySnapshot(state) {
    S.st.handNo = state.hand_no;
    S.st.street = state.street;
    S.st.streetZh = STREET_ZH[state.street] || state.street;
    S.st.board = state.board || [];
    S.st.pot = state.pot;
    S.st.complete = !!state.complete;
    S.st.seats = (state.seats || []).map(function (x) {
      return { seat: x.seat, position: x.position, stack: x.stack, bet: x.bet,
               inHand: x.in_hand, hole: x.hole,   // 公开模式 null; '??'=未知
               name: S.names[x.seat] || ('P' + (x.seat + 1)) };
    });
  }

  function pushHistory(mark) {
    const eq = {};
    for (const k in S.st.equity) eq[k] = S.st.equity[k];
    S.st.history.push({ eq: eq, mark: mark || null });
    if (S.st.history.length > 400) S.st.history.shift();
  }

  function handle(m) {
    S.st.lastSeq = m.seq || S.st.lastSeq;
    switch (m.type) {
      case 'game_event': {
        applySnapshot(m.state);
        if (m.event === 'hand_start') {
          const d = m.detail;
          Object.assign(S.st, freshState(), { connected: true });
          applySnapshot(m.state);
          S.st.handNo = d.hand_no; S.st.blinds = d.blinds;
          S.st.robot = '发牌中'; S.st.says = ['第 ' + d.hand_no + ' 手开始'];
        } else if (m.event === 'deal') {
          S.st.robot = '验证';
          if (m.detail.source) S.st.dealSources[m.detail.zone] = m.detail.source;
        } else if (m.event === 'action') {
          const d = m.detail;
          S.st.acts[d.seat] = ACT_ZH[d.action] +
            (d.amount != null && d.action !== 'fold' && d.action !== 'check'
              ? ' ' + d.amount : '');
          S.st.actor = null;
        } else if (m.event === 'settlement') {
          const pays = m.detail.payoffs;
          let w = 0;
          for (let i = 1; i < pays.length; i++) if (pays[i] > pays[w]) w = i;
          S.st.winner = w; S.st.payoffs = pays; S.st.robot = '完成';
          pushHistory('结算');
        }
        break;
      }
      case 'analysis_update': {
        const marks = { flop: '翻牌', turn: '转牌', river: '河牌' };
        const newStreet = m.street !== S.st._lastAnalysisStreet;
        S.st._lastAnalysisStreet = m.street;
        S.st.equity = {};
        (m.players || []).forEach(function (p) {
          S.st.equity[p.seat] = Math.round(p.equity.win * 100);
        });
        S.st.analysis = m;
        pushHistory(newStreet ? marks[m.street] : null);
        break;
      }
      case 'input_request':
        S.st.actor = m.seat; S.st.robot = '等待行动';
        break;
      case 'dealer_prompt':
        S.st.prompt = m.text;
        if (m.level === 'alert') S.st.alerts.push(m.text);
        break;
      case 'commentary':
        S.st.says.push(m.text);
        if (S.st.says.length > 20) S.st.says.shift();
        break;
      case 'gto_hint':
        S.st.gto[m.seat] = { node: m.node, hero: m.hero, summary: m.summary,
                             matrix: m.matrix, actions: m.actions, source: m.source };
        break;
      case 'gto_deviation':
        S.st.deviations.push(m);
        break;
      case 'audit_report':
        S.st.audits.push(m);
        S.st.robot = m.verdict === 'match' ? '视觉审计✓' : '视觉审计⚠';
        break;
      case 'alert':
        S.st.alerts.push(m.text);
        break;
    }
    if (S.cb) S.cb(S.st);
  }

  function dial() {
    try { S.ws = new WebSocket(S.url); } catch (e) { return retry(); }
    S.ws.onopen = function () { S.st.connected = true; if (S.cb) S.cb(S.st); };
    S.ws.onmessage = function (ev) {
      try { handle(JSON.parse(ev.data)); } catch (e) { /* 单条坏消息不致命 */ }
    };
    S.ws.onclose = function () { S.st.connected = false; if (S.cb) S.cb(S.st); retry(); };
    S.ws.onerror = function () { try { S.ws.close(); } catch (e) {} };
  }

  function retry() { setTimeout(dial, 2000); }

  global.TexasLive = {
    connect: function (opts) {
      S.url = opts.url; S.names = opts.names || []; S.cb = opts.onUpdate;
      S.st = freshState();
      dial();
      return { state: function () { return S.st; },
               close: function () { S.cb = null; try { S.ws.close(); } catch (e) {} } };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
