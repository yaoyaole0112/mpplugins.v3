import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const {createElementVNode:_createElementVNode,resolveComponent:_resolveComponent,createVNode:_createVNode,withCtx:_withCtx,createTextVNode:_createTextVNode,toDisplayString:_toDisplayString,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "font-weight-medium mb-2" };
const _hoisted_2 = { class: "d-flex flex-wrap ga-2 my-4" };
const _hoisted_3 = { class: "font-weight-medium mb-2" };

const {computed,onMounted,ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Page',
  props: { api: { type: Object, default: () => ({}) } },
  emits: ['close'],
  setup(__props, { emit: __emit }) {

const props = __props;
const emit = __emit;
const busy = ref(false);
const error = ref('');
const status = ref({ config: { rules: [] }, last_result: {} });
const modes = {
  record: '仅删除转移记录',
  source: '删除转移记录和源文件',
  destination: '删除转移记录和媒体库文件',
  all: '删除转移记录、源文件和媒体库文件',
};
const details = computed(() => status.value.last_result?.data?.rule_details || []);
const unwrap = result => result?.data && typeof result.success === 'boolean' ? result.data : result;

async function refresh() {
  busy.value = true;
  error.value = '';
  try {
    status.value = unwrap(await props.api.get('plugin/TransferFailureCleaner/status'));
  } catch (failure) {
    error.value = failure?.message || '读取执行记录失败，请重试';
  } finally {
    busy.value = false;
  }
}

async function run(preview) {
  if (!preview && !window.confirm('将按已保存规则执行清理，涉及文件删除时不可恢复。确认继续？')) return
  busy.value = true;
  error.value = '';
  try {
    const result = await props.api.post(`plugin/TransferFailureCleaner/${preview ? 'preview' : 'run'}`, {});
    if (result?.success === false) error.value = result.message || '执行失败，请查看日志';
    status.value = unwrap(await props.api.get('plugin/TransferFailureCleaner/status'));
  } catch (failure) {
    error.value = failure?.message || '执行失败，请查看日志';
  } finally {
    busy.value = false;
  }
}

onMounted(refresh);

return (_ctx, _cache) => {
  const _component_v_btn = _resolveComponent("v-btn");
  const _component_v_card_title = _resolveComponent("v-card-title");
  const _component_v_alert = _resolveComponent("v-alert");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");
  const _component_v_progress_linear = _resolveComponent("v-progress-linear");

  return (_openBlock(), _createBlock(_component_v_card, null, {
    default: _withCtx(() => [
      _createVNode(_component_v_card_title, { class: "d-flex align-center" }, {
        default: _withCtx(() => [
          _cache[3] || (_cache[3] = _createElementVNode("span", { class: "flex-grow-1" }, "失败整理记录清理", -1)),
          _createVNode(_component_v_btn, {
            icon: "mdi-close",
            variant: "text",
            "aria-label": "关闭",
            onClick: _cache[0] || (_cache[0] = $event => (emit('close')))
          })
        ]),
        _: 1
      }),
      _createVNode(_component_v_card_text, null, {
        default: _withCtx(() => [
          _createVNode(_component_v_alert, {
            type: "warning",
            variant: "tonal",
            class: "mb-4"
          }, {
            default: _withCtx(() => [...(_cache[4] || (_cache[4] = [
              _createTextVNode(" 以下操作使用已保存规则。涉及文件删除时不可恢复，请先预览。 ", -1)
            ]))]),
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
            : _createCommentVNode("", true),
          (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(status.value.config?.rules || [], (rule, index) => {
            return (_openBlock(), _createBlock(_component_v_card, {
              key: index,
              variant: "outlined",
              class: "mb-3"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_v_card_text, null, {
                  default: _withCtx(() => [
                    _createElementVNode("div", _hoisted_1, "规则 " + _toDisplayString(index + 1) + " · " + _toDisplayString(modes[rule.delete_mode]), 1),
                    _createElementVNode("div", null, _toDisplayString(rule.error_types?.join('；') || '未选择错误类型，不执行'), 1)
                  ]),
                  _: 2
                }, 1024)
              ]),
              _: 2
            }, 1024))
          }), 128)),
          (!status.value.config?.rules?.length)
            ? (_openBlock(), _createBlock(_component_v_alert, {
                key: 1,
                type: "info",
                variant: "tonal",
                class: "mb-4"
              }, {
                default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
                  _createTextVNode(" 尚无清理规则，请先在插件设置中添加规则并保存。 ", -1)
                ]))]),
                _: 1
              }))
            : _createCommentVNode("", true),
          _createElementVNode("div", _hoisted_2, [
            _createVNode(_component_v_btn, {
              color: "primary",
              variant: "tonal",
              disabled: busy.value,
              onClick: _cache[1] || (_cache[1] = $event => (run(true)))
            }, {
              default: _withCtx(() => [...(_cache[6] || (_cache[6] = [
                _createTextVNode("预览匹配记录（不删除）", -1)
              ]))]),
              _: 1
            }, 8, ["disabled"]),
            _createVNode(_component_v_btn, {
              color: "warning",
              disabled: busy.value,
              onClick: _cache[2] || (_cache[2] = $event => (run(false)))
            }, {
              default: _withCtx(() => [...(_cache[7] || (_cache[7] = [
                _createTextVNode("立即清理", -1)
              ]))]),
              _: 1
            }, 8, ["disabled"]),
            _createVNode(_component_v_btn, {
              variant: "text",
              disabled: busy.value,
              onClick: refresh
            }, {
              default: _withCtx(() => [...(_cache[8] || (_cache[8] = [
                _createTextVNode("刷新", -1)
              ]))]),
              _: 1
            }, 8, ["disabled"])
          ]),
          (busy.value)
            ? (_openBlock(), _createBlock(_component_v_progress_linear, {
                key: 2,
                indeterminate: "",
                class: "mb-3"
              }))
            : _createCommentVNode("", true),
          _createVNode(_component_v_alert, {
            type: "info",
            variant: "tonal",
            class: "mb-4"
          }, {
            default: _withCtx(() => [
              _createTextVNode(_toDisplayString(status.value.last_result?.time || '') + " " + _toDisplayString(status.value.last_result?.message || '尚无执行记录'), 1)
            ]),
            _: 1
          }),
          (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(details.value, (detail) => {
            return (_openBlock(), _createBlock(_component_v_card, {
              key: detail.number,
              variant: "outlined",
              class: "mb-3"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_v_card_text, null, {
                  default: _withCtx(() => [
                    _createElementVNode("div", _hoisted_3, "规则 " + _toDisplayString(detail.number) + " · " + _toDisplayString(detail.label), 1),
                    _createElementVNode("div", null, "匹配 " + _toDisplayString(detail.matched) + " 条，删除 " + _toDisplayString(detail.deleted) + " 条，跳过 " + _toDisplayString(detail.skipped) + " 条，保留 " + _toDisplayString(detail.retained) + " 条，失败 " + _toDisplayString(detail.failed) + " 条", 1),
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(detail.types, (count, type) => {
                      return (_openBlock(), _createElementBlock("div", {
                        key: type,
                        class: "mt-2"
                      }, _toDisplayString(type) + "：" + _toDisplayString(count) + " 条", 1))
                    }), 128))
                  ]),
                  _: 2
                }, 1024)
              ]),
              _: 2
            }, 1024))
          }), 128))
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
}

};

export { _sfc_main as default };
