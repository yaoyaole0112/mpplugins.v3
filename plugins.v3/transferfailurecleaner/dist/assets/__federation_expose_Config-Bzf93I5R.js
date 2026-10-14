import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,renderList:_renderList,Fragment:_Fragment,openBlock:_openBlock,createElementBlock:_createElementBlock,toDisplayString:_toDisplayString,createElementVNode:_createElementVNode,createBlock:_createBlock,createCommentVNode:_createCommentVNode,mergeProps:_mergeProps} = await importShared('vue');


const _hoisted_1 = { class: "pa-4" };
const _hoisted_2 = { class: "d-flex align-center mb-4" };
const _hoisted_3 = { class: "text-subtitle-1 font-weight-medium flex-grow-1" };
const _hoisted_4 = { class: "d-flex justify-end mt-5" };

const {computed,ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Config',
  props: {
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
},
  emits: ['save', 'layout', 'switch', 'close'],
  setup(__props, { emit: __emit }) {

const props = __props;
const emit = __emit;

const modes = [
  { value: 'record', title: '仅删除转移记录' },
  { value: 'source', title: '删除转移记录和源文件' },
  { value: 'destination', title: '删除转移记录和媒体库文件' },
  { value: 'all', title: '删除转移记录、源文件和媒体库文件' },
];
const config = props.initialConfig || {};
const options = ref(Array.isArray(config.error_options) ? config.error_options : []);
const warning = ref(config.error_warning || '');
const enabled = ref(Boolean(config.enabled));
const cron = ref(config.cron || '0 3 * * *');
const retainHours = ref(Number(config.retain_hours || 0));
const rules = ref(normalizeRules(config.rules));
const error = ref('');

function normalizeRules(value) {
  if (!Array.isArray(value)) return []
  return value.filter(rule => rule && modes.some(mode => mode.value === rule.delete_mode))
    .map(rule => ({ delete_mode: rule.delete_mode, error_types: Array.isArray(rule.error_types) ? [...rule.error_types] : [] }))
}
function addRule() {
  rules.value.push({ delete_mode: 'record', error_types: [] });
}
function removeRule(index) {
  rules.value.splice(index, 1);
}
function selectedElsewhere(index, type) {
  return rules.value.some((rule, ruleIndex) => ruleIndex !== index && rule.error_types.includes(type))
}
function validate() {
  const owners = new Map();
  for (let index = 0; index < rules.value.length; index += 1) {
    for (const type of rules.value[index].error_types) {
      if (owners.has(type)) return `错误类型“${type}”已分配到规则 ${owners.get(type) + 1}，不能重复分配`
      owners.set(type, index);
    }
  }
  return ''
}
function save() {
  error.value = validate();
  if (error.value) return
  emit('save', {
    enabled: enabled.value,
    cron: cron.value,
    retain_hours: Math.max(0, Number(retainHours.value) || 0),
    rules: rules.value.map(rule => ({ delete_mode: rule.delete_mode, error_types: [...rule.error_types] })),
  });
}
async function refreshOptions() {
  try {
    const result = await props.api.get('plugin/TransferFailureCleaner/options');
    if (result?.error_options) options.value = result.error_options;
    warning.value = result?.error_warning || '';
  } catch (refreshError) {
    warning.value = '读取失败提示失败，请稍后重试。';
  }
}

emit('layout', { maxWidth: '60rem' });
if (!rules.value.length) addRule();

return (_ctx, _cache) => {
  const _component_v_alert = _resolveComponent("v-alert");
  const _component_v_btn = _resolveComponent("v-btn");
  const _component_v_select = _resolveComponent("v-select");
  const _component_v_list_item = _resolveComponent("v-list-item");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");
  const _component_v_switch = _resolveComponent("v-switch");
  const _component_v_text_field = _resolveComponent("v-text-field");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createVNode(_component_v_alert, {
      type: "warning",
      variant: "tonal",
      class: "mb-5",
      style: {"line-height":"1.7"}
    }, {
      default: _withCtx(() => [...(_cache[3] || (_cache[3] = [
        _createTextVNode(" 文件删除不可恢复，请先预览。删除源文件会联动删除对应下载任务。 ", -1)
      ]))]),
      _: 1
    }),
    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(rules.value, (rule, index) => {
      return (_openBlock(), _createBlock(_component_v_card, {
        key: index,
        variant: "outlined",
        class: "mb-4"
      }, {
        default: _withCtx(() => [
          _createVNode(_component_v_card_text, { class: "pa-4" }, {
            default: _withCtx(() => [
              _createElementVNode("div", _hoisted_2, [
                _createElementVNode("div", _hoisted_3, "规则 " + _toDisplayString(index + 1), 1),
                (rules.value.length > 1)
                  ? (_openBlock(), _createBlock(_component_v_btn, {
                      key: 0,
                      icon: "mdi-minus",
                      size: "small",
                      variant: "tonal",
                      color: "error",
                      "aria-label": "删除规则",
                      onClick: $event => (removeRule(index))
                    }, null, 8, ["onClick"]))
                  : _createCommentVNode("", true)
              ]),
              _createVNode(_component_v_select, {
                modelValue: rule.delete_mode,
                "onUpdate:modelValue": $event => ((rule.delete_mode) = $event),
                items: modes,
                "item-title": "title",
                "item-value": "value",
                label: "删除方式",
                density: "comfortable",
                class: "mb-5",
                "hide-details": ""
              }, null, 8, ["modelValue", "onUpdate:modelValue"]),
              _createVNode(_component_v_select, {
                modelValue: rule.error_types,
                "onUpdate:modelValue": $event => ((rule.error_types) = $event),
                items: options.value,
                "item-title": "title",
                "item-value": "value",
                label: "此规则清理的错误类型",
                multiple: "",
                chips: "",
                "closable-chips": "",
                density: "comfortable",
                hint: "可选多个类型；每个错误类型只能分配到一条规则。",
                "persistent-hint": ""
              }, {
                item: _withCtx(({ props: itemProps, item }) => [
                  _createVNode(_component_v_list_item, _mergeProps({ ref_for: true }, itemProps, {
                    disabled: selectedElsewhere(index, item.value)
                  }), null, 16, ["disabled"])
                ]),
                _: 2
              }, 1032, ["modelValue", "onUpdate:modelValue", "items"])
            ]),
            _: 2
          }, 1024)
        ]),
        _: 2
      }, 1024))
    }), 128)),
    _createVNode(_component_v_btn, {
      "prepend-icon": "mdi-plus",
      variant: "tonal",
      color: "primary",
      class: "mb-5",
      onClick: addRule
    }, {
      default: _withCtx(() => [...(_cache[4] || (_cache[4] = [
        _createTextVNode(" 添加规则 ", -1)
      ]))]),
      _: 1
    }),
    _createVNode(_component_v_card, {
      variant: "outlined",
      class: "mb-5"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_card_text, { class: "pa-4" }, {
          default: _withCtx(() => [
            _cache[5] || (_cache[5] = _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium mb-3" }, "公共设置（适用于所有规则）", -1)),
            _createVNode(_component_v_switch, {
              modelValue: enabled.value,
              "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((enabled).value = $event)),
              label: "启用定时清理",
              class: "mb-3",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            _createVNode(_component_v_text_field, {
              modelValue: cron.value,
              "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((cron).value = $event)),
              label: "执行周期（Cron）",
              density: "comfortable",
              class: "mb-5",
              hint: "默认每天 03:00，使用 MP 时区。",
              "persistent-hint": ""
            }, null, 8, ["modelValue"]),
            _createVNode(_component_v_text_field, {
              modelValue: retainHours.value,
              "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((retainHours).value = $event)),
              modelModifiers: { number: true },
              label: "保留最近多少小时",
              density: "comfortable",
              type: "number",
              min: "0",
              hint: "0 不限制；设置保留时间后，时间无效的记录会跳过。",
              "persistent-hint": ""
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        })
      ]),
      _: 1
    }),
    (error.value)
      ? (_openBlock(), _createBlock(_component_v_alert, {
          key: 0,
          type: "error",
          variant: "tonal",
          class: "mb-4"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(error.value), 1)
          ]),
          _: 1
        }))
      : (warning.value)
        ? (_openBlock(), _createBlock(_component_v_alert, {
            key: 1,
            type: "info",
            variant: "tonal",
            class: "mb-4",
            style: {"line-height":"1.7"}
          }, {
            default: _withCtx(() => [
              _createTextVNode(_toDisplayString(warning.value), 1)
            ]),
            _: 1
          }))
        : _createCommentVNode("", true),
    _createVNode(_component_v_alert, {
      type: "info",
      variant: "tonal",
      style: {"line-height":"1.7"}
    }, {
      default: _withCtx(() => [...(_cache[6] || (_cache[6] = [
        _createTextVNode(" 保存后到“查看数据”按规则预览或清理。留空规则不执行。 ", -1)
      ]))]),
      _: 1
    }),
    _createElementVNode("div", _hoisted_4, [
      _createVNode(_component_v_btn, {
        "prepend-icon": "mdi-refresh",
        variant: "text",
        class: "mr-2",
        onClick: refreshOptions
      }, {
        default: _withCtx(() => [...(_cache[7] || (_cache[7] = [
          _createTextVNode("刷新错误类型", -1)
        ]))]),
        _: 1
      }),
      _createVNode(_component_v_btn, {
        "prepend-icon": "mdi-content-save",
        color: "primary",
        variant: "flat",
        onClick: save
      }, {
        default: _withCtx(() => [...(_cache[8] || (_cache[8] = [
          _createTextVNode("保存", -1)
        ]))]),
        _: 1
      })
    ])
  ]))
}
}

};

export { _sfc_main as default };
