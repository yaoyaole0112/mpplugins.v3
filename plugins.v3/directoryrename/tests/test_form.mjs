import assert from 'node:assert/strict'
import test from 'node:test'
import {
  directoryChoices, makeRule, readRules, ruleValues, serializeConfig,
  storageChoices, unwrap, validateRules,
} from '../src/form.js'

const template = '{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}}{{fileExt}}'
const values = [
  { name: '分享池', storage: '115网盘Plus', path: '/影视资源/分享池/已整理/', tv: template, movie: '{{title}}{{fileExt}}' },
  { name: '电视剧媒体库', storage: '115网盘Plus', path: '/影视资源/已整理/电视剧/', tv: template, movie: '' },
]
const config = { enabled: true, rules_json: JSON.stringify(values, null, 2), future_setting: 'retained' }

test('legacy config opens and saves without changing any rule or template', () => {
  const rules = readRules(config)
  const saved = serializeConfig(config, true, rules)
  assert.deepEqual(JSON.parse(saved.rules_json), values)
  assert.equal(saved.rules_json, config.rules_json)
  assert.equal(saved.future_setting, 'retained')
  assert.equal(config.rules_json, JSON.stringify(values, null, 2))
})

test('UI keys remain unique when copying and never enter saved rules', () => {
  const rule = makeRule(values[0])
  const copied = makeRule(rule)
  assert.notEqual(rule._key, copied._key)
  copied._tab = 'movie'
  assert.deepEqual(ruleValues([rule, copied]), [values[0], values[0]])
})

test('unknown rule fields and template whitespace are preserved', () => {
  const rule = { ...values[0], future_option: true, movie: '  {{title}}\n{{fileExt}}  ' }
  assert.deepEqual(ruleValues(readRules({ rules_json: JSON.stringify([rule]) })), [rule])
})

test('missing configuration starts empty but invalid data is never silently reset', () => {
  assert.deepEqual(readRules({}), [])
  for (const value of ['broken', '{}', '[null]', '[1]', '[{"storage": 12}]']) {
    assert.throws(() => readRules({ rules_json: value }))
  }
})

test('validation accepts existing rules and optional single media template', () => {
  assert.equal(validateRules(readRules(config), true), '')
  assert.equal(validateRules([makeRule({ ...values[0], tv: '' })], true), '')
})

test('enabled plugin cannot save no rules but disabled plugin can', () => {
  assert.match(validateRules([], true), /至少/)
  assert.equal(validateRules([], false), '')
})

test('validation rejects missing storage and unsafe paths', () => {
  for (const change of [
    { storage: ' ' }, { path: '' }, { path: 'relative' },
    { path: '/a/../b' }, { path: '/a\\b' },
    { tv: '', movie: '' },
  ]) {
    assert.notEqual(validateRules([makeRule({ ...values[0], ...change })], true), '')
  }
})

test('duplicate targets are detected across trailing and repeated slashes', () => {
  const first = makeRule(values[0])
  const second = makeRule({ ...values[0], path: '/影视资源//分享池/./已整理' })
  assert.match(validateRules([first, second], true), /重复/)
  second.storage = 'local'
  assert.equal(validateRules([first, second], true), '')
})

test('storage choices retain configured but temporarily unavailable storage', () => {
  assert.deepEqual(
    storageChoices([{ name: '本地', type: 'local' }], [values[0]]),
    [{ title: '本地', value: 'local' }, { title: '115网盘Plus', value: '115网盘Plus' }],
  )
})

test('directory suggestions are scoped by storage and deduplicated', () => {
  const directories = [
    { library_storage: '115网盘Plus', library_path: '/library' },
    { library_storage: 'local', library_path: '/local' },
    { library_storage: '115网盘Plus', library_path: '/library' },
    { library_storage: '115网盘Plus', library_path: '' },
  ]
  assert.deepEqual(directoryChoices(directories, '115网盘Plus'), [
    { title: '/library', value: '/library' },
  ])
})

test('API results support unwrapped values and native response envelopes', () => {
  const data = [{ name: 'test' }]
  assert.equal(unwrap(data), data)
  assert.equal(unwrap({ success: true, data }), data)
  assert.throws(() => unwrap({ success: false, message: '模板无效' }), /模板无效/)
})
