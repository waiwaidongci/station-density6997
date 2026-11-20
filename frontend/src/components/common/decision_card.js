import {cardStates} from '../../constants/enums.js';
import {esc,fmt,formObject} from '../../utils/format.js';
import {action,toast} from '../../hooks/actions.js';
import {cardsApi} from '../../api/research.js';

// 判定卡只展示已发布且通过逐站复算的任务；其余状态仅显示任务台账，不渲染卡片。
export function decisionCardSection(run,cards,writable){
  const clusters=(run.result?.clusters||[]).map(c=>c.cluster);
  return '<section class="decision-card-block"><div class="page-heading compact-heading"><p class="eyebrow">FIELD DECISION CARD</p><h2>复访判定卡</h2><p>选择已完成研究的目标簇与误收上限，在冻结站点上求命中最多的规则；每站至多判断两次，只比较三项原始读数与阈值，簇外站（含噪声）判中即误收。</p></div>'
    +(writable?'<form id="card-form" class="card-form"><label>目标簇<select name="target_cluster">'+clusters.map(c=>'<option value="'+c+'">分区 '+c+'（'+esc(labelOf(run,c))+'）</option>').join('')+'</select></label><label>误收上限（站）<input name="false_accept_cap" type="number" min="0" step="1" required value="8"></label><button type="submit">在冻结数据上求解 →</button><p class="fine">调低上限会取代同簇未结束的旧任务，旧解不会发布。</p></form>':'<p class="fine">阅读者可查看已发布判定卡；新建求解由分析师或负责人执行。</p>')
    +cardLedger(cards)
    +cards.filter(c=>c.state==='completed'&&c.card?.verified).map(publishedCard).join('')
    +'</section>';
}

function labelOf(run,cluster){
  const found=(run.result?.clusters||[]).find(c=>c.cluster===cluster);
  return found?found.count+' 站':'';
}

function cardLedger(cards){
  if(!cards.length)return '<p class="empty">还没有判定卡求解任务。</p>';
  return '<div class="card-ledger"><h3>求解任务台账</h3>'+cards.map(c=>{
    const p=c.progress||{};
    const progress=(c.state==='queued'||c.state==='running')&&p.total?(' · 已核验 '+fmt(p.index)+'/'+fmt(p.total)+' 个根分裂'):'';
    return '<div class="card-task"><span class="status '+esc(c.state)+'">'+esc(cardStates[c.state]||c.state)+'</span><small>#'+c.id+' · 目标簇 '+c.target_cluster+' · 误收上限 '+c.false_accept_cap+progress+'</small>'+(c.error?'<em class="card-error">'+esc(c.error)+'</em>':'')+'</div>';
  }).join('')+'</div>';
}

function publishedCard(c){
  const d=c.card;
  return '<article class="decision-card" data-card-id="'+c.id+'"><header><p class="eyebrow">拿着读数就能用</p><h3>复访判定卡 · 目标簇 '+d.target_cluster+'</h3><p class="fine">冻结站点 '+fmt(d.station_count)+' 个，其中目标簇 '+fmt(d.target_count)+' 站 · 输入校验 <code>'+esc(d.frozen_checksum||'')+'</code></p></header>'
    +'<dl class="metrics card-metrics"><div><dt>命中目标站</dt><dd>'+fmt(d.hits)+'</dd></div><div><dt>误收簇外站</dt><dd>'+fmt(d.false_accepts)+' / '+fmt(d.false_accept_cap)+'</dd></div><div><dt>判断节点</dt><dd>'+fmt(d.judgement_nodes)+'</dd></div><div><dt>每站最多判断</dt><dd>'+fmt(d.max_judgements_per_station)+' 次</dd></div></dl>'
    +'<div class="rule-tree">'+ruleNode(d.tree,true)+'</div>'
    +'<details class="card-steps"><summary>打印版线性步骤（'+d.steps.length+' 步）</summary><ol class="flat-steps">'+d.steps.map(s=>s.test?'<li class="step-judge">判断 #'+s.judge_no+'：'+esc(s.test.field_label)+'，'+esc(s.test.left)+' 走左支，'+esc(s.test.right)+' 走右支</li>':'<li class="step-leaf">'+esc(s.action)+'</li>').join('')+'</ol></details>'
    +'<p class="fine verified-line">✓ 已按规则逐站独立复算，命中 '+fmt(d.hits)+'、误收 '+fmt(d.false_accepts)+' 与搜索结果一致，判定卡方可显示。</p></article>';
}

function ruleNode(node,isRoot){
  if(node.type==='leaf'){
    return '<div class="rule-leaf '+node.decision+'"><strong>'+esc(node.action)+'</strong></div>';
  }
  return '<div class="rule-judge"><div class="judge-head"><span class="judge-no">判断 '+node.judge_no+'</span><b>'+esc(node.field_label)+'</b><code>'+esc(node.threshold_text)+'</code></div>'
    +'<div class="rule-branches"><div class="branch branch-left"><small>≤ '+esc(node.threshold_text)+'</small>'+ruleNode(node.left)+'</div><div class="branch branch-right"><small>&gt; '+esc(node.threshold_text)+'</small>'+ruleNode(node.right)+'</div></div></div>';
}

export function bindDecisionCard(runId,refresh){
  action('#card-form',async form=>{
    const data=formObject(form);
    await cardsApi.create({run_id:runId,target_cluster:Number(data.target_cluster),false_accept_cap:Number(data.false_accept_cap)});
    await refresh();toast('判定卡精确求解已入队，可在台账查看核验进度');
  });
}
