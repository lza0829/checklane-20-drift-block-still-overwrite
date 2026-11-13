<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, putJSON } from '../api'

const datasets = ref([])
const datasetId = ref(null) // null = 全局
const cfg = ref(null)
const strategy = ref('block')
const fieldsText = ref('')
const enabled = ref(true)
const audits = ref([])
const snapshots = ref([])
const selectedBatch = ref('')
const err = ref('')
const msg = ref('')

async function loadConfig() {
  const q = datasetId.value ? `?dataset_id=${datasetId.value}` : ''
  cfg.value = await getJSON(`/api/drift-guard/config${q}`)
  strategy.value = cfg.value.strategy
  enabled.value = cfg.value.enabled
  fieldsText.value = (cfg.value.compare_fields || []).join(', ')
}

async function loadAudits() {
  const q = datasetId.value ? `?dataset_id=${datasetId.value}` : ''
  audits.value = await getJSON(`/api/drift-guard/audit${q}`)
}

async function loadSnapshot() {
  if (!selectedBatch.value) {
    snapshots.value = []
    return
  }
  snapshots.value = await getJSON(`/api/drift-guard/snapshots/${selectedBatch.value}`)
}

async function save() {
  err.value = ''
  msg.value = ''
  const compare_fields = fieldsText.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  try {
    const saved = await putJSON('/api/drift-guard/config', {
      dataset_id: datasetId.value,
      enabled: enabled.value,
      strategy: strategy.value,
      compare_fields,
    })
    msg.value = `已保存到${datasetId.value ? '数据集覆盖' : '全局'}：${saved.strategy} / 比对字段 ${
      saved.compare_fields.length ? saved.compare_fields.join('、') : '全部字段'
    }`
    await loadConfig()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function fieldPairs(a) {
  return (a.changed_fields || []).map((f) => ({
    field: f,
    before: JSON.stringify(a.before?.[f]),
    after: JSON.stringify(a.after?.[f]),
  }))
}

onMounted(async () => {
  datasets.value = await getJSON('/api/datasets')
  await loadConfig()
  await loadAudits()
})
</script>

<template>
  <div class="page">
    <header>
      <h1>同键变更门禁</h1>
      <p class="muted">
        入检开始冻结本数据集已签发精选快照，按 entity_key 对照比对字段；阻断与覆盖策略互斥，同时只生效一种
      </p>
    </header>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="msg" class="tag ok">{{ msg }}</p>

    <div class="panel">
      <div class="row">
        <label>配置范围
          <select v-model="datasetId" @change="loadConfig(); loadAudits()">
            <option :value="null">全局（所有数据集）</option>
            <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.id }} · {{ d.name }}</option>
          </select>
        </label>
        <span v-if="cfg" class="tag">当前生效来源：{{ cfg.scope === 'dataset' ? '数据集覆盖' : '全局默认' }}</span>
      </div>
      <div class="row" style="margin-top:12px">
        <label><input type="checkbox" v-model="enabled" /> 启用门禁</label>
        <label>策略（互斥）
          <select v-model="strategy">
            <option value="block">阻断：变更行进隔离，不得覆盖精选</option>
            <option value="overwrite">覆盖：放行并覆盖精选，强制留审计</option>
          </select>
        </label>
      </div>
      <div style="margin-top:12px">
        <label>比对字段（逗号分隔；留空 = 对照全部字段，任一变化即变更）
          <input
            v-model="fieldsText"
            type="text"
            style="width:100%;margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:8px"
            placeholder="例如：sku, price, qty"
          />
        </label>
      </div>
      <div class="row" style="margin-top:12px">
        <button @click="save">保存配置</button>
        <span class="muted" v-if="cfg">
          现策略 {{ cfg.strategy }}，比对 {{ cfg.compare_fields.length ? cfg.compare_fields.join('、') : '全部字段' }}
        </span>
      </div>
    </div>

    <div class="panel">
      <h3>覆盖审计（变更前精选值 → 变更后本批值）</h3>
      <table v-if="audits.length">
        <thead>
          <tr><th>批次</th><th>数据集</th><th>键</th><th>变更字段明细</th><th>策略</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id">
            <td>#{{ a.batch_id }}</td>
            <td>{{ a.dataset_id }}</td>
            <td>{{ a.entity_key }}</td>
            <td>
              <div v-for="p in fieldPairs(a)" :key="p.field">
                <code>{{ p.field }}</code>：
                <span class="muted">{{ p.before }}</span>
                → <strong>{{ p.after }}</strong>
              </div>
            </td>
            <td><span class="tag" :class="a.strategy === 'overwrite' ? 'ok' : ''">{{ a.strategy }}</span></td>
            <td class="muted">{{ a.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">暂无覆盖审计（阻断策略不会产生覆盖；覆盖策略每次覆盖都会留痕）</p>
    </div>

    <div class="panel">
      <h3>入检开始冻结的精选快照</h3>
      <div class="row">
        <label>批次 ID
          <input
            v-model="selectedBatch"
            type="number"
            min="1"
            style="padding:6px;border:1px solid var(--line);border-radius:6px"
            placeholder="如 2"
          />
        </label>
        <button @click="loadSnapshot">查看快照</button>
      </div>
      <table v-if="snapshots.length" style="margin-top:10px">
        <thead>
          <tr><th>键</th><th>冻结 payload</th><th>来源批次</th><th>当时策略</th><th>比对字段</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in snapshots" :key="s.id">
            <td>{{ s.entity_key }}</td>
            <td><code>{{ JSON.stringify(s.payload) }}</code></td>
            <td>#{{ s.source_batch_id }}</td>
            <td>{{ s.strategy }}</td>
            <td class="muted">{{ s.compare_fields.length ? s.compare_fields.join('、') : '全部字段' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
