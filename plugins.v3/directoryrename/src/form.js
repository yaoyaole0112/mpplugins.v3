let nextKey = 0

export function unwrap(result) {
  if (result?.success === false) throw new Error(result.message || '请求失败')
  return result && typeof result.success === 'boolean' && 'data' in result
    ? result.data : result
}

export function makeRule(value = {}) {
  const rule = { ...value }
  for (const field of ['name', 'storage', 'path', 'tv', 'movie']) {
    if (rule[field] === undefined) rule[field] = ''
  }
  for (const field of ['storage', 'path', 'movie', 'tv']) {
    if (typeof rule[field] !== 'string') throw new Error(`规则的 ${field} 字段不是文本`)
  }
  return { ...rule, name: String(rule.name ?? ''), _key: ++nextKey, _tab: 'tv' }
}

export function readRules(config) {
  if (config?.rules_json === undefined) return []
  const parsed = JSON.parse(config.rules_json)
  if (!Array.isArray(parsed)) throw new Error('已保存的规则不是数组')
  return parsed.map(rule => {
    if (!rule || typeof rule !== 'object' || Array.isArray(rule)) {
      throw new Error('已保存的规则格式不正确')
    }
    return makeRule(rule)
  })
}

export function ruleValues(rules) {
  return rules.map(({ _key, _tab, ...value }) => ({ ...value }))
}

export function serializeConfig(initialConfig, enabled, rules) {
  const values = ruleValues(rules)
  let encoded = JSON.stringify(values, null, 2)
  try {
    if (JSON.stringify(JSON.parse(initialConfig.rules_json)) === JSON.stringify(values)) {
      encoded = initialConfig.rules_json
    }
  } catch {
    // New configurations have no serialized rules yet.
  }
  return {
    ...initialConfig,
    enabled,
    rules_json: encoded,
  }
}

function canonicalPath(path) {
  return `/${path.split('/').filter(part => part && part !== '.').join('/')}`
}

export function validateRules(rules, enabled) {
  if (enabled && rules.length === 0) return '启用时至少需要一条目标目录规则'
  const targets = new Set()
  for (const [index, rule] of rules.entries()) {
    const label = rule.name || `规则 ${index + 1}`
    if (!rule.storage.trim()) return `${label}：请选择目标存储`
    if (!rule.path.startsWith('/') || rule.path.includes('\\') || rule.path.split('/').includes('..')) {
      return `${label}：目标目录必须是绝对路径，且不能包含反斜杠或上级目录`
    }
    if (!rule.tv.trim() && !rule.movie.trim()) return `${label}：至少填写一种重命名模板`
    const target = JSON.stringify([rule.storage, canonicalPath(rule.path)])
    if (targets.has(target)) return `${label}：目标存储和目录与其他规则重复`
    targets.add(target)
  }
  return ''
}

export function storageChoices(storages, rules) {
  const items = new Map(storages.filter(item => item?.type).map(item => [
    item.type, { title: item.name || item.type, value: item.type },
  ]))
  for (const rule of rules) {
    if (rule.storage && !items.has(rule.storage)) {
      items.set(rule.storage, { title: rule.storage, value: rule.storage })
    }
  }
  return [...items.values()]
}

export function directoryChoices(directories, storage) {
  const paths = new Set()
  return directories.filter(item => {
    if (item.library_storage !== storage || !item.library_path) return false
    if (paths.has(item.library_path)) return false
    paths.add(item.library_path)
    return true
  }).map(item => ({ title: item.library_path, value: item.library_path }))
}
