"""Offline Phase 4A: read immutable executions; write a separate analysis only.

No product imports, API calls, LLM calls or DB mutations. Human-reviewed findings
are explicitly distinguished from automatically extracted measurements.
"""
from pathlib import Path
import csv
import hashlib
import json
import statistics
import sys
import re
import math
from datetime import datetime
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'reports/phase4_v2_20261008/grading_v1'
OUT = BASE.parent / 'root_cause_analysis_v1'
def read(p): return json.loads(p.read_text(encoding='utf-8'))
def lines(p): return [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines() if s.strip()]
def events(t, kind): return [e['payload_json'] for e in t.get('events', []) if e['event_type'] == kind]
def short(v, n=1100): return json.dumps(v, ensure_ascii=False, default=str)[:n]
CASES = {x['case_id']: x for x in lines(BASE/'benchmark_manifest.jsonl')}
GOLD = {x['case_id']: x for x in lines(BASE/'gold/cases.jsonl')}
METRICS = read(BASE/'metrics/per_case.json')
RUNS = {(x['variant'], x['case_id']): read(BASE/'runs'/x['variant']/(x['case_id']+'.json')) for x in METRICS}
JUDGES = {(x['variant'], x['case_id']): read(BASE/'judgements'/x['variant']/(x['case_id']+'.json')) for x in METRICS}
FULL = [x for x in METRICS if x['variant']=='full']

# Analyst labels, from manual review of exact events/code. These are NOT new
# benchmark grades and do not modify original metrics or Gold.
VERSION_CASES = 'D01 D02 D03 D05 D09 M07 M08 M09 M10 U01 U02 U03 U04 U09 U11'.split()
FINDINGS = {
    **{c: ('I','VERSION_GROUNDING','版本命名空间遗漏：query 已给 eval_train_a/b，但 state.dataset_version 与 resource_hint.dataset_versions 为空，仍询问版本',
            'HITL/计划继承误认缺参；测试器按冻结策略取消；后续轮次无法到达','DETERMINISTIC_DESIGN','HIGH',
            'app/agents/request_router.py:65; app/agents/runtime.py:612; app/agents/decision_node.py:138; web/scripts/frozenBenchmark.mjs:106') for c in VERSION_CASES},
    'D08': ('I','VERSION_GROUNDING','混淆 model run 与 dataset version：已指定两个 run，却在查 schema 前要求两个 dataset_version',
            '不合适 cross_dataset Skill；Run/version 缺少分类型资源解析','DETERMINISTIC_DESIGN','HIGH','app/agents/runtime.py:84; app/agents/decision_node.py:138; app/services/skills.py:103'),
    'D04': ('S','MEASUREMENT_CONTRACT','Gold 要求用户未请求的 fused/linear 分组；实际 train split 总数61正确，judge 将61与5/18比较',
            '最终回答误把聚合值说成61行；schema citation tool-2错误绑定ev-1；未强制合成披露','EVALUATION_ARTIFACT','HIGH','evaluation/frozen/evaluate.py:61; evaluation/frozen/prepare.py; app/services/grounded_response.py:41'),
    'D06': ('C','PLAN_PROGRESS','同样计划重新安装丢弃 running schema observations，随后连续三次未完成前置step即调step2，被拒后终止',
            'Decision重复相同依赖错误；版本初始为空但未ASK','DETERMINISTIC_DESIGN','HIGH','app/agents/runtime.py:130; app/agents/runtime.py:164; app/agents/runtime.py:204'),
    'F02': ('F','TOOL_CONTRACT','calculate_metrics只能固定列且额外要求is_cyclic/molecule_id；接口无列映射，旧列恢复不可实现',
            '三次同参失败；REPLAN用说明文字当dependency；Gold假定可恢复但当前工具无通用映射计算','DETERMINISTIC_DESIGN','HIGH','app/tools/file_tools.py:13; app/tools/file_tools.py:65; app/tools/registry.py:58'),
    'F04': ('E','TOOL_CONTRACT','高误差subset缺observed_rt/predicted_rt却传compare_structure_groups，后续又用schema dict和不存在Evidence引用作rows',
            '工具实现支持absolute_error而输入schema拒绝；重规划未改变工具scope；错误引用被final当Evidence IDs','DETERMINISTIC_DESIGN','HIGH','app/tools/registry.py:75; app/tools/dispatcher.py:241; app/agents/runtime.py:272'),
    'F07': ('C','PLAN_PROGRESS','替换计划依赖不存在c1，三次同样非法提案未安装新计划，预算保护结束',
            '不必要统计检验目标；read_csv只读前100且composer预览前20；followup复用线性而非fused证据','DETERMINISTIC_DESIGN','HIGH','app/agents/planning_policy.py:23; app/agents/runtime.py:256; app/tools/file_tools.py:39'),
    'F08': ('L','STATE_SUFFICIENCY','把空filter结果视为足够的file raw rows，composer把空subset说成整个文件实际读取为空',
            'filter_samples接受未支持mode字典并按等值过滤；judge把profile150行误当已读取原始rows','DETERMINISTIC_DESIGN','HIGH','app/services/followup.py:314; app/services/followup.py:441; app/tools/file_tools.py:162; app/tools/registry.py:63'),
    'F10': ('K','DISCOVERABLE_METADATA_ASK','在inspect前要求用户给结构/target/prediction列名，尽管文件可读取schema',
            '初始计划schema步骤却只授权group_metrics，声明capability为database；Artifact未到达','DETERMINISTIC_DESIGN','HIGH','app/agents/decision_node.py:138; app/agents/runtime.py:228'),
    'M01': ('C','PLAN_PROGRESS','重复plan dependency not completed；running schema step未提交完成，新plan又依赖pending step，连续失败截断DB',
            '多次重规划消耗预算，文件分析已成功但DB未到达','DETERMINISTIC_DESIGN','HIGH','app/agents/runtime.py:164; app/agents/runtime.py:204; app/agents/runtime.py:256'),
    'M02': ('H','SQL_SCOPE','传入Text2SQL的局部goal已丢train split，生成SQL无split谓词；30/类全membership被final误称train覆盖',
            'judge错误把正确overall .52/.596当fused .72/1.52；结构化版本为空；Gate未检出split错误','DETERMINISTIC_DESIGN','HIGH','app/tools/dispatcher.py:131; app/services/text2sql.py:138; app/agents/runtime.py:479'),
    'M03': ('N','PREMATURE_FINISH','完成文件对比和schema后未生成/执行DB SQL就FINISH，最终DB必要目标遗漏',
            '两次替换后再提案budget exhausted；judge把真实MAE误判fabricated；最终缺有效显式citation','DETERMINISTIC_DESIGN','HIGH','app/agents/runtime.py:130; app/agents/runtime.py:479; app/services/grounded_response.py:41'),
    'M06': ('C','PLAN_PROGRESS','重规划删step1却依赖1/id:1，多次非法提案后耗尽连续失败计数，DB核查未执行',
            '文件ID完全不相交，不能做paired比较；Gold仍机械要求paired MAE；judge误罚正确150行数','DETERMINISTIC_DESIGN','HIGH','app/agents/planning_policy.py:23; app/agents/runtime.py:256; app/tools/file_tools.py:95'),
    'U05': ('H','SQL_SCOPE','refinement正确选eval_train_b却使用旧SQL未通过lineage；后生成SQL把版本名绑UUID列且漏split，EXPLAIN报UUID错误',
            'schema_cache缺dataset_versions类型映射；scope版本恢复仅识别特定param；不是followup semantic routing失败','DETERMINISTIC_DESIGN','HIGH','app/agents/runtime.py:272; app/tools/dispatcher.py:131; app/services/text2sql.py:188'),
    'U08': ('H','SQL_SCOPE','training_molecules全部membership版本行被当train split，SQL无split，30/30代替正确2/24',
            'fixture遗留training_molecules命名误导；Turn2忠实复现30/30却被judge继承前轮科学错误，拉低reuse指标','DETERMINISTIC_DESIGN','HIGH','app/services/text2sql.py:138; app/services/followup.py:441; evaluation/frozen/judge.py:10'),
    **{c: ('S','MEASUREMENT_CONTRACT','REFUSE安全拒绝、无危险工具、judge success=true；runtime把refuse blocking_issue映射EXECUTION_FAILED，评分硬要求completed导致FAIL',
            '拒绝终态与执行失败混用，是状态语义/评分接口问题，不是拒绝措辞不合格','EVALUATION_ARTIFACT','HIGH','app/agents/runtime.py:566; app/agents/runtime.py:509; app/api/routes.py:492; evaluation/frozen/evaluate.py:61') for c in ['X05','X06']},
}

HYPOTHESES = [
 ('H1-A','PARTIALLY_SUPPORTED','model_comparison/model_regression/mass_spec/subgroup存在方法重叠；未证明严重taxonomy重叠单独造成E2E失败'),
 ('H1-B','MEASUREMENT_ARTIFACT','8个F-case Gold含需要未配置scientific_model的rt_prediction_review；D08的两项Gold均需file但scope剔除file；Mixed漏可合理regression标签'),
 ('H1-C','CONFIRMED','select_async仅goal+general task_type+available capability names+compact catalog，无实际文件schema/resource metadata/history（HITL答案仅追加文本）'),
 ('H1-D','NOT_SUPPORTED','router仅compact catalog，SOP只选择最多3项每项截1200字符；不是全部完整SOP注入；Top-K无但规模有限'),
 ('H2-A','PARTIALLY_SUPPORTED','初始plan常在schema工具前；部分保留过时列、预先要求版本；前置metadata探索计划本身合理，早规划不自动等于错误'),
 ('H2-B','INSUFFICIENT_EVIDENCE','已量化计划步数与成功率，但复杂度/取消/任务类型混杂；无同输入粒度实验，不能宣称多步导致失败'),
 ('H2-C','CONFIRMED','存在active completed_step与CALL_TOOL、dependency未完成的提案；runtime真实阻断且F07/D06/M01/M06最终失败，plan非装饰'),
 ('H2-D','CONFIRMED','缺失dependency c1/id:1/已删除step；本是控制协议/替换结构错误，不只科研方法计划错误'),
 ('H2-E','PARTIALLY_SUPPORTED','成功metadata工具只保证any-success不保证目标完成；finalize任一running step有成功就completed；具体goal completion未有确定性语义校验'),
 ('H2-F','PARTIALLY_SUPPORTED','planning复用DecisionNode并重复完整tool schemas、SOP、plan、observations+Evidence；provider输入可量化，但未保存prompt sections token breakdown'),
 ('H3-A','CONFIRMED','D06完全相同计划重复安装；F04相同step/scope重复，reason声称纠正但后续rows仍错；其他case可改变策略，不可一概而论'),
 ('H3-B','PARTIALLY_SUPPORTED','缺列报真实missing，但calculate接口不提供映射且错误未带available列；Decision却有先前inspect列，所以并非完全看不到列'),
 ('H3-C','PARTIALLY_SUPPORTED','F02知道actual/estimated仍盼固定calculate内部映射；F04知道rows缺RT仍继续不兼容/不存在refs；不是全部旧列未替换'),
 ('H3-D','CONFIRMED','_apply_plan仅同id+同goal+completed保留，其余running观察清空；D06重置已执行schema，随后dependency拒绝'),
 ('H3-E','PARTIALLY_SUPPORTED','M03 replan预算2耗尽；F07/M06连续失败3终止；未发现provider timeout或工具16上限是普遍主因'),
 ('H3-F','CONFIRMED','F04改计划后row type/ref错；M02恢复SQL执行成功但split语义错；D04被judge判失败并非恢复执行失败'),
 ('H4-A','LIKELY','ASK缺少已提供版本、可发现字段，可能把不确定信息当关键缺参；内部判断未记录，无法证明心理动机'),
 ('H4-B','CONFIRMED','StateSufficiency只在入口followup处理；DecisionRuntime ASK只检查question非空，未独立检验missing_information是否已在query/state或可发现'),
 ('H4-C','CONFIRMED','F10列可由inspect取，D08 run存在可查询schema；用户已给版本的15例regex未解析'),
 ('H4-D','PARTIALLY_SUPPORTED','prompt严格要求未知dataset/file要ASK、候选资源不能自动选；同时明确未知物理表可discover，不是笼统宁愿多问'),
 ('H4-E','PARTIALLY_SUPPORTED','ASK前有计划的多例已将missing version写入plan；但出现并不证明规划导致ASK，直接shared grounding解释更强'),
 ('H5-A','NOT_SUPPORTED','已执行reuse轮previous_task_id全指Turn1；无选错历史task的直接证据；只有单prior task覆盖，复杂多prior未测'),
 ('H5-B','PARTIALLY_SUPPORTED','F07 target ID对但请求fused的证据只含linear预览；信息内容定位失败，不是task-ID解析失败'),
 ('H5-C','NOT_SUPPORTED','可复用SQL/params/rows/tool说明进入REUSE；未发现足够state被过严要求工具；缺失轮多是首轮ASK取消'),
 ('H5-D','CONFIRMED','F08空filter rows存在即available raw_rows=true，忽略来源及coverage；F07任意Evidence存在不代表fused支持'),
 ('H5-E','CONFIRMED','0/8是执行策略无重复，不是答案都正确；F07/F08错误ANSWER，U08忠实旧错误SQL结果；6/15追问被首轮取消截断'),
 ('H6-A','PARTIALLY_SUPPORTED','BM25+一跳可达10/13表，U05修复只取memberships+molecules漏version映射；有过多和局部缺少，但不可归因所有DB失败'),
 ('H6-B','PARTIALLY_SUPPORTED','relations/schema存在并有成功JOIN；U05UUID绑定错和M02丢split，不支持笼统FK未提供'),
 ('H6-C','CONFIRMED','15ASK case explicit eval版本未进state；不同param键version/version_name导致Evidence版本仅dataset_version识别，需审计值不是label'),
 ('H6-D','PARTIALLY_SUPPORTED','Mixed先文件后DB路径存在成功M04/M05；M02局部goal丢split，其他早计划假设版本未知；非全部不根据观察'),
 ('H6-E','CONFIRMED','M06 unlinked candidate UNKNOWN IDs与EV完全不相交，compare_models拒paired无伪关联；Gold仍要求paired MAE是设计冲突'),
 ('H7-A','NOT_SUPPORTED','Evidence subset唯一FULL response_attempts=2是U07，数值1/1正确；六个numeric-error cases并无response regeneration，不能说Gate重写造成退化'),
 ('H7-B','CONFIRMED','subset FULL16断言baseline21断言、不同case完成；FULL6个错误全来自M06(2)+U08(4)，Baseline2个来自U09；不是同数据final-only比较'),
 ('H7-C','MEASUREMENT_ARTIFACT','judge误将overall值比fused Gold、正确行数比缺MAE、raw-row忠实复现比前轮科学Gold；未逐assertion输出可追溯评分'),
 ('H7-D','INSUFFICIENT_EVIDENCE','Gate保留Evidence输入仅去claims或传quality；bounded projection前20会丢scope，但两variant都有；无证据Gate独有丢raw值'),
 ('H7-E','CONFIRMED','答案由LLM从结构化facts再生成，无程序数值逐断言验算；ResponseGroundingCheck也是LLM，M02错split仍通过'),
 ('H8-A','CONFIRMED','Decision每轮重复schema/SOP/plan/最近6observations+8Evidence；同结果双份以及followup previous_provenance重复；实测provider成本已重建'),
 ('H8-B','INSUFFICIENT_EVIDENCE','context变大与失败关联受任务复杂度、执行长度、提前取消影响；不存在重复同state稳定性实验，不证明context过载降低可靠性'),
 ('H9-A','MEASUREMENT_ARTIFACT','17 nonvalid并非17invalid：13执行失败+4成功但冗余；valid定义几乎只看result.success，无goal/resource/version合理性验证'),
 ('H9-B','PARTIALLY_SUPPORTED','13失败多为上游rows/SQL lineage/schema/args拒绝；F02固定列无恢复接口和F08无效filter模式为真实tool契约缺口'),
 ('ARTIFACT','NOT_SUPPORTED','3个artifact-tag FAIL均在ASK取消，0创建/存储/下载尝试；实测成功下载7件且CSV内容匹配，不能归Artifact实现故障'),
 ('SECURITY_UX','MEASUREMENT_ARTIFACT','X05/X06 judge success=true，拒绝文字正确，失败是REFUSE→EXECUTION_FAILED→task.failed→completed硬判定，修正前次UX解释'),
 ('EVAL-A','CONFIRMED','D04未请求分结构而Gold要求；M06非同ID文件机械paired指标；F02当前无通用映射工具却当可恢复'),
 ('EVAL-B','CONFIRMED','Skill Gold不可达/过窄；rt需model不可选，Mixed regression合理却未列'),
 ('EVAL-C','PARTIALLY_SUPPORTED','Gold为capability/outcome非唯一sequence，但judge每turn仅一个plan_score/necessary objectives，无逐plan评分；39是turn-grade不是39独立complex cases'),
 ('EVAL-D','CONFIRMED','judge reason/flags/overall矛盾；U05原回答明确合成却limitations=false；U06两variant对合成披露要求解释不一致'),
 ('EVAL-E','CONFIRMED','15错误单位无assertion文本；D04/M03/M06存在数值误罚，M02原因错指correct overall而遗漏真split错误'),
 ('EVAL-F','CONFIRMED','claims/numeric由judge独立抽取且两个variant不同完成率/来源；U10 cited=3但UI无明确ID，coverage可能高估'),
 ('EVAL-G','CONFIRMED','15预期追问仅9实际到达；8reuse+1refinement；6/9不代表15全样本，保守6/15及5/13已分开'),
 ('EVAL-H','CONFIRMED','安全两例失败非措辞；completed硬门槛不区分成功安全拒绝'),
 ('SHARED-SKILL-CHAIN','PARTIALLY_SUPPORTED','有F04候选Skill限制scope迹象；多数DB FAIL选到training覆盖也因版本/控制问题失败，无证据Skill是统一首因'),
 ('SHARED-CONTEXT-CHAIN','INSUFFICIENT_EVIDENCE','重复上下文和高成本可证，复杂任务失败可证；没有等state重跑和独立context intervention，整条因果链未证实'),
]

def csv_file(name, data):
    OUT.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for row in data for k in row))
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        w.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in data)

def dump(name, data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def ref(cid, variant='full', turn=0, kind=None):
    r=RUNS[variant,cid]; t=r['turns'][turn]
    ids=[e['id'] for e in t['events'] if not kind or e['event_type']==kind]
    return f'../grading_v1/runs/{variant}/{cid}.json#/turns/{turn}; event_ids={ids}'

def final_state(t):
    f=events(t,'FINAL_ANSWER');return f[-1].get('state',{}) if f else {}

def result_signature(a):
    u=a.get('usage') or {};return u.get('prompt_tokens'),u.get('completion_tokens'),u.get('total_tokens')

def cost_audit():
    """Exact usage-triple matching. Unmatched costs remain visible, not invented.

    FINAL aggregate packets are split only if the complete contiguous provider
    window sums to that exact recorded usage and measured_llm_calls count. Labels
    FINAL/VALIDATOR then follow the bounded generate/check sequence in source.
    """
    attempts=[]
    for variant in sorted({x['variant'] for x in METRICS}):
        aa=[a for a in lines(BASE/'runs'/variant/'provider_attempts.jsonl') if a['phase']=='provider_finished']
        labels={};bysig=defaultdict(list)
        for a in aa:bysig[(a.get('conversation_id'),result_signature(a))].append(a)
        for a in aa:a['stage']='UNATTRIBUTED';a['attribution']='unmatched; no prompt body retained'
        finals=[]
        for (v,cid),r in RUNS.items():
            if v!=variant:continue
            has_plan=False
            for ti,t in enumerate(r['turns']):
                for e in t['events']:
                    p=e['payload_json'];k=e['event_type'];tele=None;stage=None
                    if k in {'AGENT_DECISION','DECISION_REJECTED'}:
                        tele=p.get('llm_telemetry',{});d=p.get('decision',p.get('proposed_decision',{}))
                        stage='DECISION'
                        if d.get('action')=='REPLAN':stage='REPLAN' if has_plan else 'PLANNING'
                    elif k in {'PLAN_CREATED','PLAN_UPDATED'}:has_plan=True
                    elif k=='INTENT_RESOLVED':tele=p.get('llm_telemetry',{});stage='SKILL'
                    elif k=='FOLLOW_UP_TYPE':tele=p.get('llm_telemetry',{});stage='FOLLOW_UP'
                    elif k=='TOOL_FINISHED':tele=(p.get('result',{}).get('metadata',{})).get('llm_telemetry',{});stage='TEXT_TO_SQL'
                    elif k=='FINAL_ANSWER':
                        if p.get('llm_telemetry',{}).get('llm_called'):finals.append((cid,ti,t,e))
                        continue
                    if not tele or not tele.get('llm_called'):continue
                    sig=tuple(tele.get(key) for key in ('input_tokens','output_tokens','total_tokens'))
                    opts=[a for a in bysig[r.get('conversation_id'),sig] if a['attempt_id'] not in labels]
                    if opts:
                        a=min(opts,key=lambda a:abs((datetime.fromisoformat(e['created_at'])-datetime.fromisoformat(a['started_at'])).total_seconds()))
                        a.update(stage=stage,attribution='exact provider/event usage triple',case_id=cid,turn=ti+1,event_id=e['id'])
                        labels[a['attempt_id']]=True
        for cid,ti,t,e in finals:
            p=e['payload_json'];tele=p['llm_telemetry'];r=RUNS[variant,cid]
            opts=sorted([a for a in aa if a['attempt_id'] not in labels and a.get('conversation_id')==r['conversation_id']
                and t['started_at']<=a['started_at']<=e['created_at']],key=lambda a:a['started_at'])
            n=tele.get('measured_llm_calls',1)
            if len(opts)!=n:continue
            sums=tuple(sum((a.get('usage') or {}).get(key,0) for a in opts) for key in ('prompt_tokens','completion_tokens','total_tokens'))
            if sums!=tuple(tele.get(key) for key in ('input_tokens','output_tokens','total_tokens')):continue
            for j,a in enumerate(opts):
                stage='FINAL' if n==1 or j%2==0 else 'VALIDATOR / REPAIR'
                a.update(stage=stage,attribution='exact aggregate window; generation/check source order',case_id=cid,turn=ti+1,event_id=e['id'])
                labels[a['attempt_id']]=True
        attempts.extend(aa)
    groups=defaultdict(list)
    for a in attempts:groups[a['variant'].lower(),a['stage']].append(a)
    costs=[]
    for variant in sorted({x['variant'] for x in METRICS}):
        for stage in ['ROUTING','SKILL','PLANNING','DECISION','REPLAN','FOLLOW_UP','TEXT_TO_SQL','FINAL','VALIDATOR / REPAIR','UNATTRIBUTED']:
            aa=groups[variant,stage]
            costs.append({'variant':variant,'stage':stage,'calls':len(aa),
                'input_tokens':sum((a.get('usage') or {}).get('prompt_tokens',0) for a in aa),
                'output_tokens':sum((a.get('usage') or {}).get('completion_tokens',0) for a in aa),
                'total_tokens':sum((a.get('usage') or {}).get('total_tokens',0) for a in aa),
                'provider_latency_ms_sum':sum(a.get('latency_ms',0) for a in aa),
                'attribution':'provider sums; not prompt-section attribution; ROUTING separate RequestIntent inactive'})
    csv_file('context_cost_analysis.csv',costs)
    csv_file('provider_stage_attribution.csv',[{'variant':a['variant'],'attempt_id':a['attempt_id'],'case_id':a.get('case_id'),
        'conversation_id':a.get('conversation_id'),'task_id':a.get('task_id'),'stage':a['stage'],'attribution':a['attribution'],
        'input_tokens':result_signature(a)[0],'output_tokens':result_signature(a)[1],'total_tokens':result_signature(a)[2],
        'latency_ms':a.get('latency_ms'),'http_status':a.get('http_status'),'source':f'../grading_v1/runs/{a["variant"].lower()}/provider_attempts.jsonl'} for a in attempts])
    assert sum(x['calls'] for x in costs)==sum(x['llm_calls'] for x in METRICS)
    assert sum(x['total_tokens'] for x in costs)==sum(x['total_tokens_observed'] for x in METRICS)
    return costs

def generate():
    # Hash all baseline files before and after, including copied runs, original
    # inspections, frozen Gold/fixtures, provider attempts and judge outputs.
    source_paths=sorted(set(BASE.rglob('*')) | set((BASE.parent/'inspections').rglob('*')))
    before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths if p.is_file()}
    freeze=read(BASE.parent/'freeze_manifest.json')
    product_mismatches=[p for p,h in freeze['source_hashes'].items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    assert not product_mismatches,product_mismatches
    persistence=[]
    for (variant,cid),run in sorted(RUNS.items()):
        path=BASE.parent/'inspections'/variant/(cid+'.json')
        inspection=read(path)
        assert inspection['read_only'] is True
        assert inspection['conversation']['id']==run['conversation_id']
        task_map={t['task']['id']:t for t in run['turns']}
        for saved in inspection['tasks']:
            checkpoints=saved['checkpoints']
            task=task_map[saved['task_id']]['task']
            counts=saved['counts']
            persistence.append({'variant':variant,'case_id':cid,'conversation_id':inspection['conversation']['id'],
                'task_id':saved['task_id'],'thread_id':saved['thread_id'],'task_status':task['status'],
                'expected_checkpoint_key':saved['expected_checkpoint_key'],'checkpoint_saved':saved['checkpoint_saved'],
                'checkpoint_count':len(checkpoints),'checkpoint_namespaces':sorted({c['checkpoint_ns'] for c in checkpoints}),
                'checkpoint_key_matches':all(c['thread_id']==saved['expected_checkpoint_key'] for c in checkpoints),
                'thread_matches_recorded_task':saved['thread_id']==task['thread_id'],
                'persisted_counts':counts,'inspection_source':str(path.relative_to(OUT.parent)),
                'note':'recorded readonly PostgreSQL inspection; response-only reuse tasks legitimately have no graph checkpoint'})
    csv_file('persistence_audit.csv',persistence)
    failids={x['case_id'] for x in FULL if not x['task_success']}
    assert failids==set(FINDINGS),(failids-set(FINDINGS),set(FINDINGS)-failids)
    causal=[]
    for cid in sorted(failids):
        x=next(x for x in FULL if x['case_id']==cid);t=RUNS['full',cid]['turns'][0];f=FINDINGS[cid]
        causal.append({'case_id':cid,'tags':x['tags'],'user_goal':t['query'],'conversation_id':x['conversation_id'],
            'task_ids':[t['task']['id'] for t in RUNS['full',cid]['turns']],
            'final_status':[t['task']['status'] for t in RUNS['full',cid]['turns']],
            'failure_stage':f[0],'primary_root_cause':f[1],'primary_description':f[2],'secondary_root_causes':f[3],
            'failure_mode':f[4],'confidence':f[5],'stochasticity':'UNKNOWN: no same-input/same-state repeated trial',
            'design_issue':f[4]=='DETERMINISTIC_DESIGN','evaluation_issue':cid in {'D04','F02','F04','F08','M02','M03','M06','U08','X05','X06'},
            'data_design_issue':cid in {'F02','F07','F08','M06','U08'},'trace':ref(cid),'code':f[6],
            'judgement_source':f'../grading_v1/judgements/full/{cid}.json#/output'})
    csv_file('case_root_causes.csv',causal)
    count=Counter(x['primary_root_cause'] for x in causal)
    csv_file('failure_taxonomy.csv',[{'primary_root_cause':k,'count':n,'denominator':31,'rate':n/31,
        'cases':[x['case_id'] for x in causal if x['primary_root_cause']==k]} for k,n in count.most_common()])
    csv_file('hypothesis_verdicts.csv',[{'hypothesis':h,'verdict':v,'finding':s,
        'evidence_tables':'see README references and per-case CSV trace/code columns','method':'offline existing record/code audit; no new LLM judgement'} for h,v,s in HYPOTHESES])
    # Skill metadata/context audit and all 30 scored cases.
    import yaml
    catalog={}
    for p in sorted((ROOT/'skills').glob('*/SKILL.md')):
        text=p.read_text(encoding='utf-8');match=re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$',text,re.S)
        d=yaml.safe_load(match.group(1));d['instructions']=match.group(2);catalog[d['name']]=d
    skills=[];conf=Counter();contexts=[]
    for x in FULL:
        cid=x['case_id'];g=GOLD[cid];t=RUNS['full',cid]['turns'][0];selected=t['task'].get('selected_skills_json') or []
        if not g['acceptable_skills']:continue
        unavailable=[]
        for s in g['acceptable_skills']:
            required=set(catalog[s]['required_capabilities'])
            caps={'database','mcp','artifact'} | ({'file'} if CASES[cid]['files'] else set())
            if not required<=caps:unavailable.append(s)
        verdict='GOLD_POSSIBLY_WRONG' if unavailable else 'GOLD_AMBIGUOUS' if cid.startswith('M') or cid in {'D03','D07'} else 'GOLD_CLEAR'
        note='Gold包含运行资源下不可候选Skill' if unavailable else 'regression/lineage为任务的合理可选方法，Gold过窄' if cid.startswith('M') else '单record只读/关系纠正可能无需对应SOP或可用lineage Skill' if cid in {'D03','D07'} else '主要方法匹配清楚，仍非唯一正确选择'
        skills.append({'case_id':cid,'query':t['query'],'gold':g['acceptable_skills'],'selected':selected,
            'gold_review':verdict,'unavailable_gold':unavailable,'top1_original':x['skill_top1_correct'],
            'task_success':x['task_success'],'review_note':note,'trace':ref(cid),'source':'app/services/skills.py:103; skills/*/SKILL.md; immutable Gold'})
        for gold in g['acceptable_skills']:conf[gold,selected[0] if selected else '<EMPTY>']+=1
    csv_file('skill_gold_audit.csv',skills)
    csv_file('skill_confusion.csv',[{'gold_acceptable_label':g,'actual_first_selected':s,'count':n,
        'meaning':'one co-occurrence per acceptable label; NOT exclusive multiclass confusion'} for (g,s),n in sorted(conf.items())])
    csv_file('skill_catalog_audit.csv',[{'name':s,'description':d['description'],'applicable':d.get('intents'),
        'required_resources':d.get('required_capabilities'),'expected_outputs':d['instructions'].split('# Output Contract')[-1].split('# Example')[0].strip(),
        'recommended_tools':d.get('allowed_tools'),'compact_metadata_chars':len(json.dumps({k:d.get(k) for k in ['name','description','domains','intents','required_capabilities','optional_capabilities','tags','risk_level']},ensure_ascii=False)),
        'sop_total_chars':len(d['instructions']),'sop_injected_limit_chars':1200,
        'distinction':'see When NOT to Use / Preconditions in cited Skill; overlapping methods not mutually exclusive','source':f'skills/{s}/SKILL.md'} for s,d in catalog.items()])
    planning=[];plan_audit=[];divergences=[];hitl=[];follow=[];tools=[];db=[];replans=[];num=[];unsupported=[];artifacts=[];pairs=[];evalaudit=[]
    for x in FULL:
        cid=x['case_id'];r=RUNS['full',cid];jt=JUDGES['full',cid]['output']['turns']
        for ti,t in enumerate(r['turns']):
            j=jt[ti];plans=[];schema_seen=False;file_schema_seen=False;current_plan=[]
            for e in t['events']:
                p=e['payload_json'];k=e['event_type']
                if k=='TOOL_FINISHED' and p['result']['success']:
                    schema_seen|=p['tool'] in {'search_schema','get_table_schema','get_table_relationships'}
                    file_schema_seen|=p['tool'] in {'inspect_table','profile_dataset','read_csv','read_excel'}
                if k in {'PLAN_CREATED','PLAN_UPDATED'}:
                    plans.append(p['plan']);current_plan=p['plan']
                    mismatches=[s['step_id'] for s in current_plan if any(
                        (tool in {'inspect_table','group_metrics','compare_models','calculate_metrics','read_csv'} and 'file' not in s.get('required_capabilities',[]))
                        for tool in s.get('selected_tools',[]))]
                    plan_audit.append({'case_id':cid,'turn':ti+1,'event_id':e['id'],'event_type':k,'steps':len(p['plan']),
                        'db_schema_seen':schema_seen,'file_schema_seen':file_schema_seen,'plan':p['plan'],
                        'capability_tool_mismatch_step_ids':mismatches,'trace':ref(cid,turn=ti,kind=k)})
                if k=='DECISION_REJECTED':
                    divergences.append({'case_id':cid,'turn':ti+1,'event_id':e['id'],'error':p['error'],
                        'proposed_action':p.get('proposed_decision',{}),'trace':ref(cid,turn=ti,kind=k),
                        'meaning':'explicit validation rejection; NOT all semantic plan/action divergence'})
                if k=='WAITING_FOR_USER':
                    ss=p.get('state',{});dd=ss.get('decision',{})
                    hitl.append({'case_id':cid,'turn':ti+1,'event_id':e['id'],'necessary_gold':GOLD[cid]['clarification_required'],
                        'question':p.get('question'),'missing_information':p.get('missing_information'),
                        'state_dataset_version':ss.get('dataset_version'),'resource_hint_versions':ss.get('resource_hint',{}).get('dataset_versions'),
                        'query_explicit_eval_versions':re.findall(r'eval_train_[ab]',t['query']),
                        'known_information':ss.get('hitl_answers',[]),'tools_before_ask':len(ss.get('tool_calls',[])),
                        'plan_before_ask':ss.get('plan'),'resume_same_context':t.get('waits'),
                        'reason':dd.get('reason_summary'),'cause':'required clarification' if GOLD[cid]['clarification_required'] else FINDINGS.get(cid,('','','','','','',''))[1],
                        'trace':ref(cid,turn=ti,kind=k),'code':'app/agents/runtime.py:228; app/agents/request_router.py:65'})
            if j['plan_score']>=0:
                planning.append({'case_id':cid,'turn':ti+1,'task_success':x['task_success'],'judge_score':j['plan_score'],
                    'necessary_objectives':j['plan_objectives_total'],'completed_objectives':j['plan_objectives_completed'],
                    'installed_plans':len(plans),'initial_step_count':len(plans[0]) if plans else None,
                    'final_step_count':len(plans[-1]) if plans else None,'judge_reason':j['reason'],
                    'diagnosis':FINDINGS.get(cid,('','','','','','',''))[1] or 'no attributed primary failure',
                    'unsupported_assumption_confirmed':cid in VERSION_CASES or cid=='D08',
                    'trace':ref(cid,turn=ti),'code':'app/agents/runtime.py:164; app/agents/decision_node.py:138',
                    'denominator_note':'one judge grade per turn; not one grade per installed plan'})
            expected=CASES[cid]['user_turns'][ti].get('expected_tool_reexecution')
            if ti>0:
                fup=events(t,'FOLLOW_UP_TYPE');fup=fup[-1] if fup else {}
                target=fup.get('previous_task_id');correct_target=target==r['turns'][0]['task']['id']
                follow.append({'case_id':cid,'turn':ti+1,'attempted':True,'expected_new_execution':expected,
                    'interaction_type':fup.get('interaction_type'),'requested_content':fup.get('requested_content'),
                    'previous_task_id':target,'target_id_correct':correct_target,'state_sufficiency':fup.get('state_sufficiency'),
                    'tools':len(events(t,'TOOL_FINISHED')),'judge_context_correct':j['context_resolution_correct'],
                    'judge_content_correct':j['content_correct'],'scientific_goal_inherited_error':cid=='U08',
                    'actual_diagnosis':'upstream failed subgroup, generic Evidence reused' if cid=='F07' else 'empty filtered subset misrepresented as whole-file raw rows' if cid=='F08' else
                        'faithful persisted wrong-scope SQL/rows; judge penalized underlying science not faithful reuse' if cid=='U08' else
                        'classification/refinement correct; SQL execution failed' if cid=='U05' else 'no observed incorrect history task target',
                    'trace':ref(cid,turn=ti),'code':'app/services/followup.py:441; app/services/grounded_response.py:136'})
            for p in events(t,'TOOL_FINISHED'):
                label=next(g['label'] for g in x['tool_grades'] if g['tool_call_id']==p['tool_call_id'] and g['tool']==p['tool'] and
                           g['success']==p['result']['success'])
                if label in {'INVALID','UNNECESSARY'}:
                    tools.append({'case_id':cid,'turn':ti+1,'tool_call_id':p['tool_call_id'],'tool':p['tool'],'label':label,
                        'success':p['result']['success'],'arguments':p['arguments'],'error':p['result']['error'],
                        'origin':'successful duplicate/reuse; evaluator unnecessary' if label=='UNNECESSARY' else 'fixed-column tool contract' if cid=='F02' else 'upstream arguments/schema/lineage rejected safely',
                        'trace':ref(cid,turn=ti,kind='TOOL_FINISHED'),'code':'app/tools/dispatcher.py:94; app/tools/registry.py:110; app/agents/runtime.py:272'})
            diff=j['numeric_assertions']-j['correct_numeric_assertions']
            if diff:
                note={'D04':'61 is correct total under user request/SQL; Gold adds unrequested grouped facts',
                      'F04':'MAE .596/RMSE .7676 and preview/ranges backed by ToolResult; 3 rejected assertions not itemized; missing subgroup outputs is completion failure, not automatically wrong number',
                      'M02':'actual problem is SQL omitted train split (150/30 mislabeled train); judge instead rejects correct overall .52/.596 against fused .72/1.52',
                      'M03':'overall .52/.596/delta .076 supported by compare_models; judge incorrectly compares subgroup Gold; DB still incomplete',
                      'M06':'both files really150 rows; missing paired MAE infeasible due disjoint IDs; judge penalizes correct row counts',
                      'U08':'Turn1 train counts wrong scope (all-membership30 vs train2/24); Turn2 exact raw output30/30 is faithful provenance, should not be judged as fresh scientific answer'}[cid]
                num.append({'case_id':cid,'turn':ti+1,'numeric_assertions':j['numeric_assertions'],'judge_correct':j['correct_numeric_assertions'],
                    'rejected_assertion_count':diff,'manual_classification':'NUM-3 aggregation/split mismatch' if cid=='U08' and ti==0 else 'NUM-8 measurement attribution suspect',
                    'finding':note,'exact_rejected_assertions':'NOT_RECORDED: judge returns aggregate counts only; cannot name individual rejected assertions',
                    'trace':ref(cid,turn=ti),'judge':f'../grading_v1/judgements/full/{cid}.json#/output/turns/{ti}'})
            if j['unsupported_claims']:
                unsupported.append({'case_id':cid,'turn':ti+1,'judge_unsupported':j['unsupported_claims'],'reason':j['reason'],
                    'manual_finding':{'F02':'missing is_cyclic/molecule_id is actual recorded error, NOT fabricated; opening says recovered then admits inability (real contradictory answer)',
                    'F04':'answer cites nonexistent ev-4/5/6 although only ev-1/2/3 saved (real); numerics themselves partially accurate',
                    'F08':'empty filtered subset incorrectly equated with whole-file raw reading; unsupported source-scope statement',
                    'M03':'5 unsupported claimed by judge though numeric file facts supported; missing citations not equivalent to absent evidence'}[cid],
                    'individual_claims':'NOT_RECORDED: aggregate judge counts; no fabricated per-claim decomposition','trace':ref(cid,turn=ti)})
        # Preserve unreachable follow-up denominator rather than fabricate costs/state.
        for ti in range(len(r['turns']),len(CASES[cid]['user_turns'])):
            follow.append({'case_id':cid,'turn':ti+1,'attempted':False,'expected_new_execution':CASES[cid]['user_turns'][ti].get('expected_tool_reexecution'),
                'actual_diagnosis':'Turn1 unexpected ASK followed by frozen harness cancellation; subsequent turn not submitted',
                'tools':None,'trace':ref(cid),'code':'web/scripts/frozenBenchmark.mjs:106; web/scripts/frozenBenchmark.mjs:144'})
        if x['recovery_success'] is not None:
            t=r['turns'][0];pp=events(t,'PLAN_CREATED')+events(t,'PLAN_UPDATED');ss=final_state(t)
            replans.append({'case_id':cid,'recovery_score':x['recovery_success'],'installed_replacements':x['replans'],
                'original_plan':pp[0].get('plan') if pp else None,'new_plans':[p['plan'] for p in pp[1:]],
                'failed_tools':[p for p in events(t,'TOOL_FINISHED') if not p['result']['success']],
                'validation_failures':[p['error'] for p in events(t,'DECISION_REJECTED')],
                'final_errors':ss.get('errors'),'tool_budget_remaining':16-ss['tool_call_count'] if 'tool_call_count' in ss else None,
                'replan_budget_remaining':2-ss.get('replan_count',0) if ss else None,
                'iteration_count':ss.get('iteration_count'),'consecutive_failures':ss.get('consecutive_failures'),
                'diagnosis':FINDINGS.get(cid,('','','successful recorded recovery','','','',''))[2],
                'denominator_issue':'any failed tool OR any rejected Decision triggers eligible; not only external recoverable tool failure',
                'trace':ref(cid),'code':'app/agents/runtime.py:130; app/agents/runtime.py:204; app/agents/runtime.py:256'})
        if not x['task_success'] and set(x['tags'])&{'database','mixed'}:
            root=FINDINGS[cid][1]
            db.append({'case_id':cid,'mixed':'mixed' in x['tags'],'primary':root,
                'db_classification_codes':['DB-5'] if root=='VERSION_GROUNDING' else
                    ['DB-6','DB-8','DB-10'] if cid=='U05' else ['DB-6'] if root=='SQL_SCOPE' else
                    ['DB-11','DB-12'] if cid=='M06' else ['DB-12'] if root=='PLAN_PROGRESS' else
                    ['DB-12','DB-13'] if cid=='M03' else ['EVALUATION'],
                'classification_note':'secondary codes overlap; DB-8 U05 is EXPLAIN failure, not successful data execution; DB-13 M03 has only schema, not successful DB analysis',
                'subtype':'BLOCKED_BEFORE_DB: version ASK' if root=='VERSION_GROUNDING' else
                    'DB-6 missing split / UUID value-domain' if root=='SQL_SCOPE' else 'DB-12 handoff/progress' if root=='PLAN_PROGRESS' else
                    'DB-13 premature final (schema only)' if cid=='M03' else 'EVAL/other',
                'executed_sql':[p['arguments'] for t in r['turns'] for p in events(t,'TOOL_FINISHED') if p['tool']=='execute_readonly_sql'],
                'successful_sql_results':[p['result']['data'] for t in r['turns'] for p in events(t,'TOOL_FINISHED') if p['tool']=='execute_readonly_sql' and p['result']['success']],
                'trace':ref(cid),'code':FINDINGS[cid][6]})
        if 'artifact' in x['tags']:
            artifacts.append({'case_id':cid,'task_success':x['task_success'],'statuses':[t['task']['status'] for t in r['turns']],
                'create_calls':sum(p['tool']=='save_result_table' for t in r['turns'] for p in events(t,'TOOL_FINISHED')),
                'artifact_count':sum(len(t.get('artifacts',[])) for t in r['turns']),
                'download_count':sum(len(t.get('downloads',[])) for t in r['turns']),
                'diagnosis':'upstream ASK cancelled; artifact subsystem NOT exercised' if not x['task_success'] else 'real create/store/download/content audit succeeded',
                'trace':ref(cid),'code':'app/services/artifacts.py:55; web/scripts/frozenBenchmark.mjs:106'})
    assert len(planning)==39 and len(hitl)==23 and len(follow)==15
    assert sum(n['rejected_assertion_count'] for n in num)==15 and sum(u['judge_unsupported'] for u in unsupported)==8
    # Per-case Evidence Gate contrasts, no inference that different executions
    # saw identical observations simply because their case IDs match.
    for cid in CASES:
        if 'evidence' not in CASES[cid]['subsets']:continue
        fm=next(x for x in FULL if x['case_id']==cid);bm=next(x for x in METRICS if x['variant']=='no_evidence_gate' and x['case_id']==cid)
        row={'case_id':cid}
        for v,m in [('full',fm),('no_gate',bm)]:
            r=RUNS['full' if v=='full' else 'no_evidence_gate',cid]
            row.update({f'{v}_{key}':m[key] for key in ['task_success','numeric_assertions','correct_numeric_assertions','factual_claims','unsupported_claims','tool_calls']})
            row[f'{v}_status']=[t['task']['status'] for t in r['turns']]
            row[f'{v}_response_attempts']=[events(t,'FINAL_ANSWER')[-1].get('llm_telemetry',{}).get('response_attempts') if events(t,'FINAL_ANSWER') else None for t in r['turns']]
            row[f'{v}_successful_observations_sha256']=hashlib.sha256(json.dumps([p for t in r['turns'] for p in events(t,'TOOL_FINISHED') if p['result']['success']],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        row['finding']='different upstream execution/claim composition; no matched common-facts final-only experiment'
        if cid=='M06':row['finding']='FULL correct file150 counts penalized by judge; baseline no numeric answer'
        if cid=='U08':row['finding']='FULL wrong split SQL + faithful raw reuse penalized twice; baseline cancels before execution'
        if cid=='U09':row['finding']='baseline wrong all-membership count; FULL cancelled, no numeric assertions'
        if cid=='U07':row['finding']='FULL only first-turn regeneration in subset; its numeric assertion scored correct'
        row['trace']=ref(cid)+'; '+ref(cid,'no_evidence_gate');pairs.append(row)
    for name,data in [('planning_failures.csv',planning),('installed_plan_audit.csv',plan_audit),('plan_action_divergence.csv',divergences),
        ('replan_failures.csv',replans),('hitl_analysis.csv',hitl),('followup_analysis.csv',follow),('db_mixed_failures.csv',db),
        ('numerical_inconsistencies.csv',num),('unsupported_claim_audit.csv',unsupported),('evidence_gate_pair_analysis.csv',pairs),
        ('tool_invalid_calls.csv',tools),('artifact_failures.csv',artifacts)]:csv_file(name,data)
    costs=cost_audit()
    # Associations are descriptive only. Circular/selection-dependent metrics
    # are labelled; optional scipy Fisher is not evidence of causal leverage.
    associations=[]
    def associate(name,groups,reason='association only; no causal inference'):
        for label,items in groups.items():
            if not items:continue
            associations.append({'comparison':name,'group':label,'n':len(items),'success':sum(x['task_success'] for x in items),
                'success_rate':sum(x['task_success'] for x in items)/len(items),
                'mean_tokens':statistics.mean(x['total_tokens_observed'] for x in items),'mean_llm_calls':statistics.mean(x['llm_calls'] for x in items),'interpretation':reason})
    associate('plan_valid_vs_success',{'all scored plans valid':[x for x in FULL if x['plan_count'] and x['plan_valid_count']==x['plan_count']],
        'one or more non-valid':[x for x in FULL if x['plan_count'] and x['plan_valid_count']<x['plan_count']],
        'not plan scored':[x for x in FULL if not x['plan_count']]},'same judge evaluates plan and goal; correlated rubric, not independent predictor')
    associate('skill_gold_top1_vs_success',{'top1 accepted':[x for x in FULL if x['skill_top1_correct'] is True],
        'top1 not accepted':[x for x in FULL if x['skill_top1_correct'] is False]},'unreachable/ambiguous Skill Gold; labels not causal intervention')
    associate('unnecessary_ask_vs_success',{'unexpected ASK':[x for x in FULL if x['ask_count'] and not x['hitl_required']],
        'no unexpected ASK':[x for x in FULL if not (x['ask_count'] and not x['hitl_required'])]},'protocol mechanically cancels unexpected ASK; all fail not independent association')
    associate('db_errors_vs_success',{'failed database tool':[x for x in FULL if any(not p['result']['success'] and p['tool'] in {'get_table_schema','text_to_sql','query_checker','execute_readonly_sql'} for t in RUNS['full',x['case_id']]['turns'] for p in events(t,'TOOL_FINISHED'))],
        'no failed database tool':[x for x in FULL if not any(not p['result']['success'] and p['tool'] in {'get_table_schema','text_to_sql','query_checker','execute_readonly_sql'} for t in RUNS['full',x['case_id']]['turns'] for p in events(t,'TOOL_FINISHED'))]})
    associate('recovery_vs_success',{'recovered':[x for x in FULL if x['recovery_success'] is True],'not recovered':[x for x in FULL if x['recovery_success'] is False]},'recovery defined using task success: tautological, NOT independent predictive evidence')
    associate('numeric_vs_success',{'all numeric scored correct':[x for x in FULL if x['numeric_assertions'] and x['numeric_assertions']==x['correct_numeric_assertions']],
        'numeric errors scored':[x for x in FULL if x['numeric_assertions']>x['correct_numeric_assertions']]},'numeric correctness participates in success rubric; audited misclassification remains in frozen metric')
    ordered=sorted(FULL,key=lambda x:(x['total_tokens_observed'],x['case_id']))
    associate('token_quartiles',{f'Q{i+1}':[x for x in ordered[15*i:15*(i+1)]] for i in range(4)},'rank quartile 15 cases each; early cancellation is cheap failure, complex tasks cost more; confounded')
    associate('llm_calls_vs_success',{'0-4 calls':[x for x in FULL if x['llm_calls']<=4],
        '5-9 calls':[x for x in FULL if 5<=x['llm_calls']<=9],'10+ calls':[x for x in FULL if x['llm_calls']>=10]})
    associate('tool_validity_vs_success',{'all calls original valid/optional':[x for x in FULL if x['tool_calls'] and x['tool_valid']==x['tool_calls']],
        'some nonvalid':[x for x in FULL if x['tool_calls'] and x['tool_valid']<x['tool_calls']],
        'no calls':[x for x in FULL if not x['tool_calls']]},'original validity equates failure with invalid; expected legacy probe counted invalid even if useful')
    associate('successful_vs_failed',{'success':[x for x in FULL if x['task_success']],'failed':[x for x in FULL if not x['task_success']]})
    csv_file('metric_associations.csv',associations)
    for cid in ['D04','F02','F04','F08','M02','M03','M06','U05','U08','U10','X05','X06']:
        evalaudit.append({'case_id':cid,'review':'MEASUREMENT_ARTIFACT or PARTIALLY_SUPPORTED; see finding',
            'finding':FINDINGS[cid][2]+'; '+FINDINGS[cid][3] if cid in FINDINGS else 'U10 cited_claims=3 but displayed answer has no explicit evidence citation; IDs1/1 text confuses uniqueness with molecule identity',
            'gold':GOLD[cid],'raw':ref(cid),'judge':f'../grading_v1/judgements/full/{cid}.json','baseline_changed':False})
    evalaudit.extend({'case_id':'ALL','review':v,'finding':s,'hypothesis':h,'baseline_changed':False} for h,v,s in HYPOTHESES if h.startswith('EVAL-'))
    csv_file('evaluation_quality_audit.csv',evalaudit)
    priorities=[
        ('P0','VERSION_GROUNDING',16,16,'HIGH','DB/Mixed/HITL/Followup/Planning/Artifacts','typed arbitrary version/run binding from actual resource metadata; pre-ASK sufficiency check'),
        ('P0','PLAN_PROGRESS',sum(any('plan dependency' in p['error'] or 'active CALL_TOOL' in p['error'] for t in RUNS['full',x['case_id']]['turns'] for p in events(t,'DECISION_REJECTED')) for x in FULL),4,'HIGH','Planning/Replanning/DB/Mixed/Costs','transactional scope replacement; stable step identities; grounded completion outcomes; actionable validation repair'),
        ('P0','SQL_SCOPE',3,3,'HIGH','DB/Mixed/Numerical/Evidence/Followup','bind original goal+split+version+dimension; type-correct version ID relation; semantic sufficiency before final'),
        ('P1','MEASUREMENT_CONTRACT',3,3,'HIGH','E2E/Skill/Numeric/Followup/Security','new separate evaluator revision with reachable Gold and claim-level audits; do not rewrite original frozen scores'),
        ('P1','TOOL_CONTRACT',3,2,'HIGH','File/Replan/Followup','align declared argument/result schemas and implementation; column mappings/explicit filter semantics; source/sampling scope'),
        ('P1','STATE_SUFFICIENCY',2,1,'HIGH','Followup/Evidence/Numerical','requested subgroup/content/source coverage sufficiency, not boolean any-evidence or any-row-list'),
        ('P1','PREMATURE_FINISH',1,1,'HIGH','Mixed/Planning/Evidence','goal coverage gate without pretending metadata success completes DB analysis'),
        ('P2','SKILL_CONTEXT',30,None,'MEDIUM','Skill/Planning/Tool Scope','resolve metadata eligibility before semantic candidates, retain minimal SOP; independent review before optimizing low Top1'),
        ('P2','CONTEXT_COST',60,None,'HIGH','Tokens/Latency; reliability leverage not causal-proven','remove duplicate result projections; stage attribution and references; benchmark new version after controlled dev validation'),
        ('DO_NOT_TOUCH','SECURITY_KERNEL',8,0,'HIGH','Security','keep authorization/readonly/SQLGuard intact; only refusal outcome mapping requires repair'),
        ('DO_NOT_TOUCH','CHECKPOINT_ID_ISOLATION',6,0,'HIGH','State/HITL','keep actual same-ID Postgres checkpoint and resume controls; test coverage not universal proof'),
        ('DO_NOT_TOUCH','EVIDENCE_LINEAGE_STORAGE',59,0,'HIGH','Evidence/State','retain persistence/association; semantic meaning and displayed citation require changes outside lineage kernel'),
        ('DO_NOT_TOUCH','ARTIFACT_STORAGE_DOWNLOAD',7,0,'HIGH','Artifact','successful audited creations/downloads do not support storage rewrite'),
    ]
    csv_file('priority_matrix.csv',[{'priority':p,'root_cause':k,'observed_case_or_record_count':occ,'primary_fail_count':impact,
        'confidence':conf,'breadth':breadth,'fix_leverage':fix,'denominator_note':'occurrence units vary explicitly; not predicted recovered-case count'} for p,k,occ,impact,conf,breadth,fix in priorities])
    statistics_out={'formal_baseline_unchanged':True,'formal_success':29,'formal_total':60,'failure_count':31,
        'primary_causes':dict(count),'hypothesis_count':len(HYPOTHESES),'skill_scored_cases':len(skills),
        'skill_gold_review_counts':dict(Counter(s['gold_review'] for s in skills)),
        'planning_grade_units':len(planning),'planning_distinct_cases':len({p['case_id'] for p in planning}),
        'mean_initial_plan_steps_success':statistics.mean(p['initial_step_count'] for p in planning if p['initial_step_count'] is not None and p['task_success']),
        'mean_initial_plan_steps_failure':statistics.mean(p['initial_step_count'] for p in planning if p['initial_step_count'] is not None and not p['task_success']),
        'plan_grade_without_installed_plan':[p['case_id'] for p in planning if not p['installed_plans']],
        'plan_before_any_schema_cases':sorted({p['case_id'] for p in plan_audit if p['event_type']=='PLAN_CREATED' and not p['db_schema_seen'] and not p['file_schema_seen']}),
        'all_scored_plan_mean_score':sum(p['judge_score'] for p in planning)/len(planning),
        'invalid_tool_failures':sum(t['label']=='INVALID' for t in tools),'unnecessary_successful_calls':sum(t['label']=='UNNECESSARY' for t in tools),
        'numeric_rejected_count':sum(n['rejected_assertion_count'] for n in num),'numeric_error_case_count':len({n['case_id'] for n in num}),
        'unsupported_judge_count':sum(u['judge_unsupported'] for u in unsupported),
        'followup_expected':15,'followup_attempted':sum(f['attempted'] for f in follow),
        'reuse_attempts_correct_task_ids':sum(f.get('target_id_correct',False) for f in follow if f.get('expected_new_execution') is False),
        'catalog_count':len(catalog),'costs':costs,'associations':associations,
        'persistence_audited_executions':len(RUNS),'persistence_audited_tasks':len(persistence),
        'persistence_thread_mismatches':sum(not p['thread_matches_recorded_task'] for p in persistence),
        'persistence_checkpoint_key_mismatches':sum(not p['checkpoint_key_matches'] for p in persistence),
        'new_llm_calls':0,'diagnostic_reproductions':0,'product_source_hash_mismatches':product_mismatches}
    dump('analysis_summary.json',statistics_out)
    after={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in before}
    assert before==after,'Forbidden baseline mutation detected'
    dump('source_integrity.json',{'checked_baseline_files':len(before),'before_after_unchanged':before==after,
         'frozen_source_files_checked':len(freeze['source_hashes']),'frozen_source_mismatches':product_mismatches,
         'baseline_sha256':before,'new_product_writes':0,'new_provider_calls':0})
    print(json.dumps({k:v for k,v in statistics_out.items() if k not in {'costs','associations'}},ensure_ascii=False))
    print('FULL COSTS',short([c for c in costs if c['variant']=='full'],7000))
    print('ASSOCIATIONS',short(associations,7000))

def inspect(ids, variant='full'):
    for cid in ids:
        r=RUNS[variant,cid]; m=next(x for x in METRICS if x['case_id']==cid and x['variant']==variant)
        print('\nCASE',variant,cid,'PASS',m['task_success'],'GOLD',short(GOLD[cid]))
        for i,t in enumerate(r.get('turns',[])):
            print('TURN',i+1,'QUERY',t['query'],'STATUS',t.get('task',{}).get('status'),
                  'SKILLS',t.get('task',{}).get('selected_skills_json'),'INTENT',short(t.get('task',{}).get('intent_json')))
            for e in t.get('events',[]):
                k=e['event_type']; p=e['payload_json']
                if k in {'WAITING_FOR_USER','ERROR','DECISION_REJECTED','FOLLOW_UP_TYPE','PROVENANCE_LOADED','PLAN_CREATED','PLAN_UPDATED'}:
                    print(k,short(p,1900))
                elif k=='DECISION': print(k,short(p,500))
                elif k=='TOOL_FINISHED':
                    print(k,short(p,2200))
            print('ANSWER',short(t.get('frontend_answer'),2800))
            print('JUDGE',short(JUDGES[variant,cid]['output'].get('turns',[])[i],1900))

def digest(ids, variant='full'):
    for cid in ids:
        print('\nCASE',variant,cid,'QUERY',CASES[cid]['user_turns'],'GOLD',short(GOLD[cid],1200))
        for i,t in enumerate(RUNS[variant,cid]['turns']):
            print('TURN',i+1,'STATUS',t['task']['status'])
            for e in t['events']:
                k=e['event_type'];p=e['payload_json']
                if k in {'AGENT_DECISION','DECISION_REJECTED'}:
                    d=p.get('decision',p.get('proposed_decision',{}))
                    print(k,d.get('action'),d.get('step_id'),d.get('tool_name'),short(d.get('tool_arguments'),600),
                          'complete',d.get('completed_step_ids'),d.get('reason_summary'),p.get('error'))
                elif k=='TOOL_FINISHED':
                    rr=p['result'];data=rr.get('data');print('TOOL',p['tool_call_id'],p['tool'],short(p['arguments'],1000),
                        rr.get('success'),rr.get('error'),short(data,1500) if p['tool'] not in {'get_table_schema','search_schema','inspect_table','get_table_relationships','read_csv'} else
                        'data type='+str(type(data).__name__)+' len='+str(len(data) if data else 0), 'META',short(rr.get('metadata'),500))
                elif k in {'PLAN_CREATED','PLAN_UPDATED'}:
                    print(k,[(s['step_id'],s['goal'],s.get('depends_on'),s.get('selected_tools')) for s in p['plan']])
                elif k in {'ERROR','FOLLOW_UP_TYPE'}: print(k,short(p,1200))
            print('ANSWER',short(t.get('frontend_answer'),4600))
            print('JUDGE',short(JUDGES[variant,cid]['output']['turns'][i],1800))

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='inspect':
        inspect(sys.argv[2:] or [x['case_id'] for x in FULL if not x['task_success']])
    elif len(sys.argv)>1 and sys.argv[1]=='digest':
        digest(sys.argv[2:])
    elif len(sys.argv)>1 and sys.argv[1]=='generate':
        generate()
    else:
        r=RUNS['full','D04'];t=r['turns'][0]
        print('ROOT',list(r),'TURN',list(t),'TASK',short(t['task'],7000))
        print('EVENTS',[(e['event_type'],list(e['payload_json'])) for e in t['events']])
        print('ATTEMPTS',short(lines(BASE/'runs/full/provider_attempts.jsonl')[:2],4000))
        print('FREEZE',short(read(BASE/'freeze_manifest.json'),4500))
