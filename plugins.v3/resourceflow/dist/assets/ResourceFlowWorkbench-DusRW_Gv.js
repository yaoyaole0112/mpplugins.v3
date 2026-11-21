import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, c as cloneTask, n as normalizeTask, f as formatBytes, u as unwrapResponse, t as taskStateMeta, a as formatDateTime, b as formatDuration, d as torrentProgress, e as normalizeSettings } from './_plugin-vue_export-helper-DLGS7ItP.js';

const {unref:_unref$2,toDisplayString:_toDisplayString$3,createTextVNode:_createTextVNode$2,resolveComponent:_resolveComponent$3,withCtx:_withCtx$2,createVNode:_createVNode$3,createElementVNode:_createElementVNode$3,openBlock:_openBlock$3,createBlock:_createBlock$2,createCommentVNode:_createCommentVNode$3,withModifiers:_withModifiers$1} = await importShared('vue');


const _hoisted_1$3 = { class: "editor-section" };
const _hoisted_2$3 = { class: "editor-section__head" };
const _hoisted_3$3 = { class: "editor-switches" };
const _hoisted_4$2 = { class: "editor-section" };
const _hoisted_5$2 = { class: "editor-section" };
const _hoisted_6$2 = { class: "editor-section" };
const _hoisted_7$2 = { class: "editor-switches" };
const _hoisted_8$2 = { class: "editor-recent-rule" };
const _hoisted_9$2 = { class: "editor-section" };
const _hoisted_10$2 = { class: "editor-section" };
const _hoisted_11$2 = { class: "editor-section" };
const _hoisted_12$2 = { class: "editor-section" };
const _hoisted_13$2 = { class: "editor-section__head" };
const _hoisted_14$2 = { class: "text-subtitle-1 font-weight-medium" };
const _hoisted_15$2 = { class: "text-body-2 text-medium-emphasis" };
const _hoisted_16$2 = { class: "editor-section" };
const _hoisted_17$2 = { class: "editor-section" };
const _hoisted_18$2 = { class: "editor-switches" };

const {computed: computed$2,ref: ref$2,watch: watch$2} = await importShared('vue');

const {useDisplay} = await importShared('vuetify');


const _sfc_main$3 = {
  __name: 'TaskEditorDialog',
  props: {
  modelValue: { type: Boolean, default: false },
  task: { type: Object, default: () => ({}) },
  sites: { type: Array, default: () => [] },
  downloaders: { type: Array, default: () => [] },
  globalDynamicDelete: { type: Boolean, default: false },
  saving: { type: Boolean, default: false },
},
  emits: ['update:modelValue', 'save', 'dedup-settings'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;
const display = useDisplay();
const formRef = ref$2(null);
const activeTab = ref$2('base');
const localTask = ref$2(cloneTask());

const dialogTitle = computed$2(() => (localTask.value.id ? '编辑刷流任务' : '新建刷流任务'));
// 避免 Vuetify 输入组件的 tag 属性名冲突导致下载器标签显示或写回丢失。
const downloaderTag = computed$2({
  get: () => localTask.value.tag || '',
  set: value => {
    localTask.value.tag = String(value || '').trim() || null;
  },
});
const siteName = computed$2(() => props.sites.find(item => item.value === Number(localTask.value.site_id))?.title || '未选择');
const scheduleText = computed$2(() => localTask.value.cron || `每 ${localTask.value.brush_interval || 10} 分钟`);

// 每次打开弹窗都从服务端任务快照重新创建本地草稿。
watch$2(
  () => props.modelValue,
  visible => {
    if (!visible) return
    localTask.value = cloneTask(props.task);
    activeTab.value = 'base';
  },
);

// 关闭编辑器并丢弃尚未保存的草稿。
function closeDialog() {
  emit('update:modelValue', false);
}

// 校验必填项后提交标准化任务数据。
async function saveTask() {
  const result = await formRef.value?.validate();
  if (result && !result.valid) return
  emit('save', normalizeTask(localTask.value));
}

return (_ctx, _cache) => {
  const _component_VToolbarTitle = _resolveComponent$3("VToolbarTitle");
  const _component_VSpacer = _resolveComponent$3("VSpacer");
  const _component_VBtn = _resolveComponent$3("VBtn");
  const _component_VToolbar = _resolveComponent$3("VToolbar");
  const _component_VDivider = _resolveComponent$3("VDivider");
  const _component_VTab = _resolveComponent$3("VTab");
  const _component_VTabs = _resolveComponent$3("VTabs");
  const _component_VChip = _resolveComponent$3("VChip");
  const _component_VTextField = _resolveComponent$3("VTextField");
  const _component_VCol = _resolveComponent$3("VCol");
  const _component_VSelect = _resolveComponent$3("VSelect");
  const _component_VRow = _resolveComponent$3("VRow");
  const _component_VSwitch = _resolveComponent$3("VSwitch");
  const _component_VWindowItem = _resolveComponent$3("VWindowItem");
  const _component_VBtnToggle = _resolveComponent$3("VBtnToggle");
  const _component_VWindow = _resolveComponent$3("VWindow");
  const _component_VSheet = _resolveComponent$3("VSheet");
  const _component_VForm = _resolveComponent$3("VForm");
  const _component_VCardText = _resolveComponent$3("VCardText");
  const _component_VCard = _resolveComponent$3("VCard");
  const _component_VDialog = _resolveComponent$3("VDialog");

  return (_openBlock$3(), _createBlock$2(_component_VDialog, {
    "model-value": __props.modelValue,
    scrollable: "",
    fullscreen: _unref$2(display).smAndDown.value,
    "max-width": "74rem",
    "onUpdate:modelValue": _cache[52] || (_cache[52] = value => emit('update:modelValue', value))
  }, {
    default: _withCtx$2(() => [
      _createVNode$3(_component_VCard, { class: "resourceflow-editor" }, {
        default: _withCtx$2(() => [
          _createVNode$3(_component_VToolbar, {
            color: "transparent",
            density: "comfortable",
            class: "resourceflow-editor__toolbar"
          }, {
            default: _withCtx$2(() => [
              _createVNode$3(_component_VToolbarTitle, null, {
                default: _withCtx$2(() => [
                  _createTextVNode$2(_toDisplayString$3(dialogTitle.value), 1)
                ]),
                _: 1
              }),
              _createVNode$3(_component_VSpacer),
              _createVNode$3(_component_VBtn, {
                color: "primary",
                variant: "flat",
                "prepend-icon": "mdi-content-save",
                loading: __props.saving,
                onClick: saveTask
              }, {
                default: _withCtx$2(() => [...(_cache[53] || (_cache[53] = [
                  _createTextVNode$2(" 保存任务 ", -1)
                ]))]),
                _: 1
              }, 8, ["loading"]),
              _createVNode$3(_component_VBtn, {
                icon: "mdi-close",
                variant: "text",
                "aria-label": "关闭",
                onClick: closeDialog
              })
            ]),
            _: 1
          }),
          _createVNode$3(_component_VDivider),
          _createVNode$3(_component_VCardText, { class: "resourceflow-editor__body" }, {
            default: _withCtx$2(() => [
              _createVNode$3(_component_VForm, {
                ref_key: "formRef",
                ref: formRef,
                class: "resourceflow-editor__form",
                onSubmit: _withModifiers$1(saveTask, ["prevent"])
              }, {
                default: _withCtx$2(() => [
                  _createVNode$3(_component_VTabs, {
                    modelValue: activeTab.value,
                    "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((activeTab).value = $event)),
                    direction: _unref$2(display).mdAndUp.value ? 'vertical' : 'horizontal',
                    color: "primary",
                    class: "resourceflow-editor__tabs"
                  }, {
                    default: _withCtx$2(() => [
                      _createVNode$3(_component_VTab, {
                        value: "base",
                        "prepend-icon": "mdi-calendar-clock"
                      }, {
                        default: _withCtx$2(() => [...(_cache[54] || (_cache[54] = [
                          _createTextVNode$2("基础与调度", -1)
                        ]))]),
                        _: 1
                      }),
                      _createVNode$3(_component_VTab, {
                        value: "selection",
                        "prepend-icon": "mdi-filter-cog-outline"
                      }, {
                        default: _withCtx$2(() => [...(_cache[55] || (_cache[55] = [
                          _createTextVNode$2("选种规则", -1)
                        ]))]),
                        _: 1
                      }),
                      _createVNode$3(_component_VTab, {
                        value: "limits",
                        "prepend-icon": "mdi-gauge"
                      }, {
                        default: _withCtx$2(() => [...(_cache[56] || (_cache[56] = [
                          _createTextVNode$2("运行限额", -1)
                        ]))]),
                        _: 1
                      }),
                      _createVNode$3(_component_VTab, {
                        value: "delete",
                        "prepend-icon": "mdi-delete-clock-outline"
                      }, {
                        default: _withCtx$2(() => [...(_cache[57] || (_cache[57] = [
                          _createTextVNode$2("删种规则", -1)
                        ]))]),
                        _: 1
                      }),
                      _createVNode$3(_component_VTab, {
                        value: "advanced",
                        "prepend-icon": "mdi-tune-variant"
                      }, {
                        default: _withCtx$2(() => [...(_cache[58] || (_cache[58] = [
                          _createTextVNode$2("高级", -1)
                        ]))]),
                        _: 1
                      })
                    ]),
                    _: 1
                  }, 8, ["modelValue", "direction"]),
                  _createVNode$3(_component_VDivider, {
                    vertical: _unref$2(display).mdAndUp.value
                  }, null, 8, ["vertical"]),
                  _createVNode$3(_component_VWindow, {
                    modelValue: activeTab.value,
                    "onUpdate:modelValue": _cache[51] || (_cache[51] = $event => ((activeTab).value = $event)),
                    touch: false,
                    class: "resourceflow-editor__window"
                  }, {
                    default: _withCtx$2(() => [
                      _createVNode$3(_component_VWindowItem, { value: "base" }, {
                        default: _withCtx$2(() => [
                          _createElementVNode$3("section", _hoisted_1$3, [
                            _createElementVNode$3("header", _hoisted_2$3, [
                              _cache[60] || (_cache[60] = _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "任务身份"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "每个任务绑定一个站点和下载器")
                              ], -1)),
                              _createVNode$3(_component_VChip, {
                                size: "small",
                                color: "primary",
                                variant: "tonal"
                              }, {
                                default: _withCtx$2(() => [...(_cache[59] || (_cache[59] = [
                                  _createTextVNode$2("必填", -1)
                                ]))]),
                                _: 1
                              })
                            ]),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.name,
                                      "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((localTask.value.name) = $event)),
                                      label: "任务名称",
                                      rules: [value => !!String(value || '').trim() || '请输入任务名称']
                                    }, null, 8, ["modelValue", "rules"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VSelect, {
                                      modelValue: localTask.value.site_id,
                                      "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((localTask.value.site_id) = $event)),
                                      items: __props.sites,
                                      label: "站点",
                                      rules: [value => !!value || '请选择站点']
                                    }, null, 8, ["modelValue", "items", "rules"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VSelect, {
                                      modelValue: localTask.value.downloader,
                                      "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((localTask.value.downloader) = $event)),
                                      items: __props.downloaders,
                                      label: "下载器",
                                      rules: [value => !!value || '请选择下载器']
                                    }, null, 8, ["modelValue", "items", "rules"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.save_path,
                                      "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((localTask.value.save_path) = $event)),
                                      label: "保存目录",
                                      placeholder: "留空使用下载器默认目录"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            }),
                            _createElementVNode$3("div", _hoisted_3$3, [
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.enabled,
                                "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((localTask.value.enabled) = $event)),
                                label: "启用任务",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"]),
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.notify,
                                "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((localTask.value.notify) = $event)),
                                label: "发送通知",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"]),
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.spec_dedup_enabled,
                                "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((localTask.value.spec_dedup_enabled) = $event)),
                                label: "共享下载审批",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"])
                            ]),
                            (localTask.value.spec_dedup_enabled)
                              ? (_openBlock$3(), _createBlock$2(_component_VBtn, {
                                  key: 0,
                                  "prepend-icon": "mdi-tune-variant",
                                  class: "editor-dedup-settings",
                                  variant: "text",
                                  size: "small",
                                  onClick: _cache[8] || (_cache[8] = $event => (emit('dedup-settings')))
                                }, {
                                  default: _withCtx$2(() => [...(_cache[61] || (_cache[61] = [
                                    _createTextVNode$2("设置全局去重规则", -1)
                                  ]))]),
                                  _: 1
                                }))
                              : _createCommentVNode$3("", true)
                          ]),
                          _createElementVNode$3("section", _hoisted_4$2, [
                            _cache[62] || (_cache[62] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "刷新计划"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "刷流刷新和下载状态检查分别调度")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.brush_interval,
                                      "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((localTask.value.brush_interval) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "1",
                                      max: "1440",
                                      label: "刷流刷新周期（分钟）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.check_interval,
                                      "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => ((localTask.value.check_interval) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "1",
                                      max: "1440",
                                      label: "状态检查周期（分钟）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.cron,
                                      "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((localTask.value.cron) = $event)),
                                      label: "CRON 表达式",
                                      placeholder: "留空使用固定刷新周期"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.active_time_range,
                                      "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((localTask.value.active_time_range) = $event)),
                                      label: "开启时间段",
                                      placeholder: "如 00:00-08:00"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            })
                          ]),
                          _createElementVNode$3("section", _hoisted_5$2, [
                            _cache[63] || (_cache[63] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "站点分享率控制"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "根据最新站点统计自动等待或恢复刷流")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VSwitch, {
                              modelValue: localTask.value.site_ratio_control,
                              "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((localTask.value.site_ratio_control) = $event)),
                              label: "启用站点分享率控制",
                              color: "primary",
                              "hide-details": "",
                              inset: ""
                            }, null, 8, ["modelValue"]),
                            (localTask.value.site_ratio_control)
                              ? (_openBlock$3(), _createBlock$2(_component_VRow, { key: 0 }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VCol, {
                                      cols: "12",
                                      md: "6"
                                    }, {
                                      default: _withCtx$2(() => [
                                        _createVNode$3(_component_VTextField, {
                                          modelValue: localTask.value.site_ratio_target,
                                          "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((localTask.value.site_ratio_target) = $event)),
                                          modelModifiers: { number: true },
                                          type: "number",
                                          min: "0.01",
                                          step: "0.01",
                                          label: "目标分享率",
                                          rules: [value => Number(value) > 0 || '请输入大于 0 的目标分享率']
                                        }, null, 8, ["modelValue", "rules"])
                                      ]),
                                      _: 1
                                    })
                                  ]),
                                  _: 1
                                }))
                              : _createCommentVNode$3("", true)
                          ])
                        ]),
                        _: 1
                      }),
                      _createVNode$3(_component_VWindowItem, { value: "selection" }, {
                        default: _withCtx$2(() => [
                          _createElementVNode$3("section", _hoisted_6$2, [
                            _cache[64] || (_cache[64] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "来源与促销"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "沿用站点列表页或 RSS 获取链路")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VSelect, {
                                      modelValue: localTask.value.freeleech,
                                      "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((localTask.value.freeleech) = $event)),
                                      label: "促销",
                                      items: [
                        { title: '全部（包括普通）', value: '' },
                        { title: '免费', value: 'free' },
                        { title: '2X 免费', value: '2xfree' },
                      ]
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VSelect, {
                                      modelValue: localTask.value.hr,
                                      "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((localTask.value.hr) = $event)),
                                      label: "排除 H&R",
                                      items: [
                        { title: '是', value: 'yes' },
                        { title: '否', value: 'no' },
                      ]
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            }),
                            _createElementVNode$3("div", _hoisted_7$2, [
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.rss_support,
                                "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((localTask.value.rss_support) = $event)),
                                label: "使用 RSS",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"]),
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.except_subscribe,
                                "onUpdate:modelValue": _cache[18] || (_cache[18] = $event => ((localTask.value.except_subscribe) = $event)),
                                label: "排除订阅",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"]),
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.site_hr_active,
                                "onUpdate:modelValue": _cache[19] || (_cache[19] = $event => ((localTask.value.site_hr_active) = $event)),
                                label: "全站 H&R",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"])
                            ]),
                            _createElementVNode$3("div", _hoisted_8$2, [
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.recent_episodes_only,
                                "onUpdate:modelValue": _cache[20] || (_cache[20] = $event => ((localTask.value.recent_episodes_only) = $event)),
                                class: "editor-recent-rule__switch",
                                label: "仅下载最近播出剧集",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"]),
                              (localTask.value.recent_episodes_only)
                                ? (_openBlock$3(), _createBlock$2(_component_VTextField, {
                                    key: 0,
                                    modelValue: localTask.value.recent_episodes_days,
                                    "onUpdate:modelValue": _cache[21] || (_cache[21] = $event => ((localTask.value.recent_episodes_days) = $event)),
                                    modelModifiers: { number: true },
                                    class: "editor-recent-rule__days",
                                    type: "number",
                                    min: "1",
                                    max: "365",
                                    step: "1",
                                    label: "最近播出天数",
                                    suffix: "天",
                                    "hide-details": ""
                                  }, null, 8, ["modelValue"]))
                                : _createCommentVNode$3("", true)
                            ]),
                            _createVNode$3(_component_VSelect, {
                              modelValue: localTask.value.blacklist_site_ids,
                              "onUpdate:modelValue": _cache[22] || (_cache[22] = $event => ((localTask.value.blacklist_site_ids) = $event)),
                              items: __props.sites,
                              label: "种子黑名单",
                              multiple: "",
                              chips: "",
                              "closable-chips": "",
                              clearable: "",
                              hint: "所选站点之间按完整种子名称互相排重；当前任务站点需包含在选择中",
                              "persistent-hint": ""
                            }, null, 8, ["modelValue", "items"]),
                            _createVNode$3(_component_VSelect, {
                              modelValue: localTask.value.download_types,
                              "onUpdate:modelValue": _cache[23] || (_cache[23] = $event => ((localTask.value.download_types) = $event)),
                              items: [
                    { title: '电影', value: 'movie' },
                    { title: '剧集', value: 'tv' },
                    { title: '短剧', value: 'short_drama' },
                    { title: '综艺', value: 'variety' },
                    { title: '动漫', value: 'anime' },
                    { title: '运动 / 体育', value: 'sports' },
                    { title: '纪录片', value: 'documentary' },
                    { title: 'MV', value: 'mv' },
                    { title: '音乐', value: 'music' },
                  ],
                              label: "下载类型",
                              multiple: "",
                              chips: "",
                              "closable-chips": "",
                              clearable: "",
                              hint: "留空不限制；选择后仅下载匹配类型，无法识别的种子将被排除",
                              "persistent-hint": ""
                            }, null, 8, ["modelValue"])
                          ]),
                          _createElementVNode$3("section", _hoisted_9$2, [
                            _cache[65] || (_cache[65] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "候选过滤"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "范围字段支持单值或“最小值-最大值”")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.size,
                                      "onUpdate:modelValue": _cache[24] || (_cache[24] = $event => ((localTask.value.size) = $event)),
                                      label: "种子大小（GB）",
                                      placeholder: "10-80"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seeder,
                                      "onUpdate:modelValue": _cache[25] || (_cache[25] = $event => ((localTask.value.seeder) = $event)),
                                      label: "做种人数",
                                      placeholder: "1-10"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.pubtime,
                                      "onUpdate:modelValue": _cache[26] || (_cache[26] = $event => ((localTask.value.pubtime) = $event)),
                                      label: "发布时间（分钟）",
                                      placeholder: "5-120"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.timezone_offset,
                                      "onUpdate:modelValue": _cache[27] || (_cache[27] = $event => ((localTask.value.timezone_offset) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      label: "站点时区偏移（小时）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "8"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.include,
                                      "onUpdate:modelValue": _cache[28] || (_cache[28] = $event => ((localTask.value.include) = $event)),
                                      label: "包含规则",
                                      placeholder: "支持正则表达式"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, { cols: "12" }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.exclude,
                                      "onUpdate:modelValue": _cache[29] || (_cache[29] = $event => ((localTask.value.exclude) = $event)),
                                      label: "排除规则",
                                      placeholder: "支持正则表达式"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            })
                          ])
                        ]),
                        _: 1
                      }),
                      _createVNode$3(_component_VWindowItem, { value: "limits" }, {
                        default: _withCtx$2(() => [
                          _createElementVNode$3("section", _hoisted_10$2, [
                            _cache[66] || (_cache[66] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "新增任务上限"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "达到任一上限后停止为当前任务新增种子")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.disksize,
                                      "onUpdate:modelValue": _cache[30] || (_cache[30] = $event => ((localTask.value.disksize) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "保种体积（GB）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.maxdlcount,
                                      "onUpdate:modelValue": _cache[31] || (_cache[31] = $event => ((localTask.value.maxdlcount) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "同时下载任务数"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.maxupspeed,
                                      "onUpdate:modelValue": _cache[32] || (_cache[32] = $event => ((localTask.value.maxupspeed) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "总上传带宽（KB/s）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.maxdlspeed,
                                      "onUpdate:modelValue": _cache[33] || (_cache[33] = $event => ((localTask.value.maxdlspeed) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "总下载带宽（KB/s）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            })
                          ]),
                          _createElementVNode$3("section", _hoisted_11$2, [
                            _cache[67] || (_cache[67] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "单种限速"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "只作用于当前任务新添加的种子")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.up_speed,
                                      "onUpdate:modelValue": _cache[34] || (_cache[34] = $event => ((localTask.value.up_speed) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "上传限速（KB/s）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.dl_speed,
                                      "onUpdate:modelValue": _cache[35] || (_cache[35] = $event => ((localTask.value.dl_speed) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "下载限速（KB/s）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            })
                          ])
                        ]),
                        _: 1
                      }),
                      _createVNode$3(_component_VWindowItem, { value: "delete" }, {
                        default: _withCtx$2(() => [
                          _createElementVNode$3("section", _hoisted_12$2, [
                            _createElementVNode$3("header", _hoisted_13$2, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", _hoisted_14$2, _toDisplayString$3(__props.globalDynamicDelete ? '全局删种托管' : '删除模式'), 1),
                                _createElementVNode$3("div", _hoisted_15$2, _toDisplayString$3(__props.globalDynamicDelete ? '选择此任务是否参加全局阈值兜底淘汰' : '动态模式会在超过体积阈值后按现有算法托管删种'), 1)
                              ])
                            ]),
                            _createVNode$3(_component_VBtnToggle, {
                              modelValue: localTask.value.proxy_delete,
                              "onUpdate:modelValue": _cache[36] || (_cache[36] = $event => ((localTask.value.proxy_delete) = $event)),
                              mandatory: "",
                              color: "primary",
                              divided: ""
                            }, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VBtn, { value: false }, {
                                  default: _withCtx$2(() => [
                                    _createTextVNode$2(_toDisplayString$3(__props.globalDynamicDelete ? '不参与托管' : '按条件删除'), 1)
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VBtn, { value: true }, {
                                  default: _withCtx$2(() => [
                                    _createTextVNode$2(_toDisplayString$3(__props.globalDynamicDelete ? '参与全局托管' : '动态删种'), 1)
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            }, 8, ["modelValue"]),
                            (localTask.value.proxy_delete && !__props.globalDynamicDelete)
                              ? (_openBlock$3(), _createBlock$2(_component_VRow, { key: 0 }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VCol, { cols: "12" }, {
                                      default: _withCtx$2(() => [
                                        _createVNode$3(_component_VTextField, {
                                          modelValue: localTask.value.delete_size_range,
                                          "onUpdate:modelValue": _cache[37] || (_cache[37] = $event => ((localTask.value.delete_size_range) = $event)),
                                          label: "动态删种阈值（GB）",
                                          placeholder: "如 350-500"
                                        }, null, 8, ["modelValue"])
                                      ]),
                                      _: 1
                                    })
                                  ]),
                                  _: 1
                                }))
                              : _createCommentVNode$3("", true)
                          ]),
                          _createElementVNode$3("section", _hoisted_16$2, [
                            _cache[68] || (_cache[68] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "触发条件"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "普通模式满足任一条件即删除")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seed_time,
                                      "onUpdate:modelValue": _cache[38] || (_cache[38] = $event => ((localTask.value.seed_time) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "做种时间（小时）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.hr_seed_time,
                                      "onUpdate:modelValue": _cache[39] || (_cache[39] = $event => ((localTask.value.hr_seed_time) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "H&R 做种时间（小时）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seed_ratio,
                                      "onUpdate:modelValue": _cache[40] || (_cache[40] = $event => ((localTask.value.seed_ratio) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "分享率"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seed_size,
                                      "onUpdate:modelValue": _cache[41] || (_cache[41] = $event => ((localTask.value.seed_size) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "上传量（GB）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.download_time,
                                      "onUpdate:modelValue": _cache[42] || (_cache[42] = $event => ((localTask.value.download_time) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "下载超时（小时）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seed_inactivetime,
                                      "onUpdate:modelValue": _cache[43] || (_cache[43] = $event => ((localTask.value.seed_inactivetime) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "未活动时间（分钟）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "4"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.seed_avgspeed,
                                      "onUpdate:modelValue": _cache[44] || (_cache[44] = $event => ((localTask.value.seed_avgspeed) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "平均上传速度（KB/s）"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "8"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.delete_except_tags,
                                      "onUpdate:modelValue": _cache[45] || (_cache[45] = $event => ((localTask.value.delete_except_tags) = $event)),
                                      label: "删除排除标签"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            }),
                            _createVNode$3(_component_VSwitch, {
                              modelValue: localTask.value.del_no_free,
                              "onUpdate:modelValue": _cache[46] || (_cache[46] = $event => ((localTask.value.del_no_free) = $event)),
                              label: "删除促销过期的未完成下载",
                              color: "primary",
                              "hide-details": "",
                              inset: ""
                            }, null, 8, ["modelValue"])
                          ])
                        ]),
                        _: 1
                      }),
                      _createVNode$3(_component_VWindowItem, { value: "advanced" }, {
                        default: _withCtx$2(() => [
                          _createElementVNode$3("section", _hoisted_17$2, [
                            _cache[69] || (_cache[69] = _createElementVNode$3("header", { class: "editor-section__head" }, [
                              _createElementVNode$3("div", null, [
                                _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "下载器适配"),
                                _createElementVNode$3("div", { class: "text-body-2 text-medium-emphasis" }, "保留原有分类、提示跳过和自动归档能力")
                              ])
                            ], -1)),
                            _createVNode$3(_component_VRow, null, {
                              default: _withCtx$2(() => [
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.qb_category,
                                      "onUpdate:modelValue": _cache[47] || (_cache[47] = $event => ((localTask.value.qb_category) = $event)),
                                      label: "qBittorrent 分类"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, {
                                  cols: "12",
                                  md: "6"
                                }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: localTask.value.auto_archive_days,
                                      "onUpdate:modelValue": _cache[48] || (_cache[48] = $event => ((localTask.value.auto_archive_days) = $event)),
                                      modelModifiers: { number: true },
                                      type: "number",
                                      min: "0",
                                      label: "自动归档天数"
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                }),
                                _createVNode$3(_component_VCol, { cols: "12" }, {
                                  default: _withCtx$2(() => [
                                    _createVNode$3(_component_VTextField, {
                                      modelValue: downloaderTag.value,
                                      "onUpdate:modelValue": _cache[49] || (_cache[49] = $event => ((downloaderTag).value = $event)),
                                      label: "下载器标签（可选）",
                                      placeholder: "默认：资源刷流-站点名",
                                      hint: "下载器中的种子标签，留空自动使用「资源刷流-站点名」；同一站点存在多个任务时请设置为不同标签",
                                      "persistent-hint": ""
                                    }, null, 8, ["modelValue"])
                                  ]),
                                  _: 1
                                })
                              ]),
                              _: 1
                            }),
                            _createElementVNode$3("div", _hoisted_18$2, [
                              _createVNode$3(_component_VSwitch, {
                                modelValue: localTask.value.site_skip_tips,
                                "onUpdate:modelValue": _cache[50] || (_cache[50] = $event => ((localTask.value.site_skip_tips) = $event)),
                                label: "自动跳过下载提示",
                                color: "primary",
                                "hide-details": "",
                                inset: ""
                              }, null, 8, ["modelValue"])
                            ])
                          ])
                        ]),
                        _: 1
                      })
                    ]),
                    _: 1
                  }, 8, ["modelValue"]),
                  _createVNode$3(_component_VSheet, {
                    tag: "aside",
                    class: "resourceflow-editor__summary"
                  }, {
                    default: _withCtx$2(() => [
                      _cache[79] || (_cache[79] = _createElementVNode$3("div", { class: "text-subtitle-1 font-weight-medium" }, "配置摘要", -1)),
                      _createElementVNode$3("dl", null, [
                        _createElementVNode$3("div", null, [
                          _cache[70] || (_cache[70] = _createElementVNode$3("dt", null, "站点", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(siteName.value), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[71] || (_cache[71] = _createElementVNode$3("dt", null, "下载器", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.downloader || '未选择'), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[72] || (_cache[72] = _createElementVNode$3("dt", null, "刷新", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(scheduleText.value), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[73] || (_cache[73] = _createElementVNode$3("dt", null, "检查", -1)),
                          _createElementVNode$3("dd", null, "每 " + _toDisplayString$3(localTask.value.check_interval || 5) + " 分钟", 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[74] || (_cache[74] = _createElementVNode$3("dt", null, "时段", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.active_time_range || '全天'), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[75] || (_cache[75] = _createElementVNode$3("dt", null, "目标分享率", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.site_ratio_control ? localTask.value.site_ratio_target || '未设置' : '关闭'), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[76] || (_cache[76] = _createElementVNode$3("dt", null, "促销", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.freeleech === '2xfree' ? '2X 免费' : localTask.value.freeleech === 'free' ? '免费' : '全部'), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[77] || (_cache[77] = _createElementVNode$3("dt", null, "保种上限", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.disksize ? `${localTask.value.disksize} GB` : '不限'), 1)
                        ]),
                        _createElementVNode$3("div", null, [
                          _cache[78] || (_cache[78] = _createElementVNode$3("dt", null, "删除", -1)),
                          _createElementVNode$3("dd", null, _toDisplayString$3(localTask.value.proxy_delete ? '动态删种' : '按条件删除'), 1)
                        ])
                      ])
                    ]),
                    _: 1
                  })
                ]),
                _: 1
              }, 512)
            ]),
            _: 1
          })
        ]),
        _: 1
      })
    ]),
    _: 1
  }, 8, ["model-value", "fullscreen"]))
}
}

};
const TaskEditorDialog = /*#__PURE__*/_export_sfc(_sfc_main$3, [['__scopeId',"data-v-fa0451bf"]]);

const {resolveComponent:_resolveComponent$2,createVNode:_createVNode$2,openBlock:_openBlock$2,createElementBlock:_createElementBlock$2,createCommentVNode:_createCommentVNode$2,toDisplayString:_toDisplayString$2,createElementVNode:_createElementVNode$2} = await importShared('vue');


const _hoisted_1$2 = { class: "shared-settings__pagination" };
const _hoisted_2$2 = {
  key: 0,
  class: "shared-settings__pagination-control"
};
const _hoisted_3$2 = { class: "shared-settings__total text-caption text-medium-emphasis" };


const _sfc_main$2 = {
  __name: 'SharedRecordPagination',
  props: {
  modelValue: { type: Number, required: true },
  total: { type: Number, required: true },
},
  emits: ['update:modelValue'],
  setup(__props, { emit: __emit }) {


const emit = __emit;

return (_ctx, _cache) => {
  const _component_VPagination = _resolveComponent$2("VPagination");

  return (_openBlock$2(), _createElementBlock$2("div", _hoisted_1$2, [
    (__props.total > 20)
      ? (_openBlock$2(), _createElementBlock$2("div", _hoisted_2$2, [
          _createVNode$2(_component_VPagination, {
            "model-value": __props.modelValue,
            length: Math.ceil(__props.total / 20),
            "total-visible": 3,
            "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => (emit('update:modelValue', $event)))
          }, null, 8, ["model-value", "length"])
        ]))
      : _createCommentVNode$2("", true),
    _createElementVNode$2("span", _hoisted_3$2, "共 " + _toDisplayString$2(__props.total) + " 条", 1)
  ]))
}
}

};
const SharedRecordPagination = /*#__PURE__*/_export_sfc(_sfc_main$2, [['__scopeId',"data-v-4485a688"]]);

const specLabels = { resolution: '分辨率', effects: '特效', codec: '编码', source: '来源', fps: '帧率', size: '大小' };
const qualityKeys = Object.keys(specLabels).filter(key => key !== 'size');

function episodeLabel(row) {
  if (row.media_type === 'movie') return '电影'
  if (!Number.isInteger(row.season)) return '季集待确认'
  const episodes = [...new Set((row.episodes || []).filter(Number.isInteger))].sort((a, b) => a - b);
  const groups = [];
  for (const episode of episodes) {
    const last = groups.at(-1);
    if (last && episode === last[1] + 1) last[1] = episode;
    else groups.push([episode, episode]);
  }
  const pad = value => String(value).padStart(2, '0');
  return `S${pad(row.season)}${groups.map(([first, last]) =>
    `E${pad(first)}${first === last ? '' : `-${pad(last)}`}`).join(',')}`
}

function releaseDetails(row) {
  const identity = [row.media_title, episodeLabel(row), row.tmdb_id ? `TMDB ${row.tmdb_id}` : '身份待确认'];
  const specs = Object.entries(specLabels).flatMap(([key, label]) => {
    const value = row.specs?.[key];
    if (value === undefined || value === null || value === '') return []
    return [`${label} ${key === 'size' ? formatBytes(value) : key === 'fps' ? `${value} FPS` : value}`]
  });
  return [...identity.filter(Boolean), ...specs].join(' · ')
}

function requestMediaSummaries(row) {
  const groups = new Map();
  for (const [index, file] of (row.files || []).entries()) {
    const key = JSON.stringify([
      file.media_type, file.tmdb_id || `unknown-${index}`, file.season,
      ...qualityKeys.map(field => file.specs?.[field] === '' ? null : file.specs?.[field] ?? null),
    ]);
    if (!groups.has(key)) groups.set(key, { file, episodes: new Set(), sizes: new Map() });
    const group = groups.get(key);
    for (const episode of file.episodes || []) group.episodes.add(episode);
    if (!group.file.media_title && file.media_title) group.file = file;
    // A physical multi-episode file contributes its size once, not once per episode.
    const physicalFile = file.path || file.id || index;
    const size = file.specs?.size;
    const validSize = typeof size === 'number' && Number.isFinite(size) && size > 0;
    const previous = group.sizes.get(physicalFile);
    if (!group.sizes.has(physicalFile)) group.sizes.set(physicalFile, validSize ? size : null);
    else if (previous !== size) group.sizes.set(physicalFile, null);
  }
  const summaries = [...groups.values()].map(group => {
    const sizes = [...group.sizes.values()];
    const total = sizes.every(value => value !== null) ? sizes.reduce((sum, value) => sum + value, 0) : null;
    const size = Number.isFinite(total) && total > 0 ? total : null;
    return releaseDetails({
      ...group.file, episodes: [...group.episodes], specs: { ...group.file.specs, size },
    })
  });
  return [...new Set(summaries)]
}

function uniqueRequestRows(items) {
  const requests = new Map();
  for (const row of items) {
    if (!row.request_id) continue
    const previous = requests.get(row.request_id);
    if (!previous || Number(row.updated || 0) > Number(previous.updated || 0)) {
      requests.set(row.request_id, row);
    }
  }
  return [...requests.values()]
}

function claimLinkSummary(row) {
  const [first, ...rest] = new Set((row.claim_links || []).filter(Boolean));
  return first ? `${first}${rest.length ? ` 等${rest.length + 1}个` : ''}` : ''
}

function rejectionSummaries(row) {
  const reasons = new Map();
  for (const item of row.rejected || []) {
    const reason = item.reason || '';
    const status = reason === '同规格已占位或已完成'
      ? ['reserved', 'queued'].includes(item.state) ? '命中记录：正在下载' : '命中记录：已完成'
      : '';
    if (!reason && !item.owner) continue
    const key = JSON.stringify([status, reason]);
    if (!reasons.has(key)) reasons.set(key, { status, reason, owners: new Set() });
    if (item.owner) reasons.get(key).owners.add(item.owner);
  }
  const summaries = [...reasons.values()].map(({ status, reason, owners }) =>
    [status, owners.size ? `下载方：${[...owners].join('、')}` : '', reason && !status ? reason : '']
      .filter(Boolean).join(' · '));
  if (row.phase === 'rejected' && row.error && ![...reasons.values()].some(item => item.reason)) {
    summaries.push(row.error);
  }
  return summaries
}

const {toDisplayString:_toDisplayString$1,createTextVNode:_createTextVNode$1,resolveComponent:_resolveComponent$1,withCtx:_withCtx$1,openBlock:_openBlock$1,createBlock:_createBlock$1,createCommentVNode:_createCommentVNode$1,createElementVNode:_createElementVNode$1,createVNode:_createVNode$1,createElementBlock:_createElementBlock$1,Fragment:_Fragment$1,renderList:_renderList$1,unref:_unref$1,normalizeClass:_normalizeClass$1} = await importShared('vue');


const _hoisted_1$1 = { class: "shared-settings__grid" };
const _hoisted_2$1 = {
  key: 1,
  class: "shared-settings__grid"
};
const _hoisted_3$1 = {
  key: 2,
  class: "shared-settings__grid shared-settings__credentials"
};
const _hoisted_4$1 = {
  key: 3,
  class: "shared-settings__grid"
};
const _hoisted_5$1 = {
  key: 4,
  class: "shared-settings__grid"
};
const _hoisted_6$1 = { class: "shared-settings__status" };
const _hoisted_7$1 = {
  key: 0,
  class: "shared-settings__search"
};
const _hoisted_8$1 = {
  key: 1,
  class: "shared-settings__records"
};
const _hoisted_9$1 = {
  key: 0,
  class: "text-body-2 text-medium-emphasis"
};
const _hoisted_10$1 = { class: "shared-settings__record-head" };
const _hoisted_11$1 = {
  key: 0,
  class: "shared-settings__heading"
};
const _hoisted_12$1 = { key: 1 };
const _hoisted_13$1 = ["data-tone"];
const _hoisted_14$1 = {
  key: 0,
  class: "shared-settings__torrent-title"
};
const _hoisted_15$1 = { class: "shared-settings__meta text-caption" };
const _hoisted_16$1 = { key: 0 };
const _hoisted_17$1 = { key: 1 };
const _hoisted_18$1 = {
  key: 1,
  class: "shared-settings__error text-body-2"
};
const _hoisted_19$1 = {
  key: 2,
  class: "shared-settings__error text-body-2"
};
const _hoisted_20$1 = { class: "shared-settings__ids" };
const _hoisted_21$1 = { class: "shared-settings__id text-caption" };
const _hoisted_22$1 = {
  key: 0,
  class: "shared-settings__id text-caption"
};
const _hoisted_23$1 = {
  key: 2,
  class: "shared-settings__records"
};
const _hoisted_24$1 = {
  key: 0,
  class: "text-body-2 text-medium-emphasis"
};
const _hoisted_25$1 = { class: "shared-settings__record-head" };
const _hoisted_26$1 = { class: "shared-settings__heading" };
const _hoisted_27$1 = { class: "shared-settings__preview shared-settings__media-summary shared-settings__info text-body-2" };
const _hoisted_28$1 = ["data-tone"];
const _hoisted_29$1 = { class: "shared-settings__torrent-title" };
const _hoisted_30$1 = { class: "shared-settings__meta shared-settings__claim-meta text-caption" };
const _hoisted_31$1 = { class: "shared-settings__claim-owner" };
const _hoisted_32$1 = { class: "shared-settings__ids" };
const _hoisted_33$1 = { class: "shared-settings__id text-caption" };
const _hoisted_34$1 = {
  key: 0,
  class: "shared-settings__id text-caption"
};

const {computed: computed$1,onMounted: onMounted$1,onUnmounted: onUnmounted$1,ref: ref$1,watch: watch$1} = await importShared('vue');


const _sfc_main$1 = {
  __name: 'SharedSettingsPanel',
  props: {
  api: { type: Object, required: true },
  pluginBase: { type: String, required: true },
  mode: { type: String, default: 'records' },
  recordTab: { type: String, default: 'requests' },
},
  emits: ['host-change', 'policy-synced', 'loading-change'],
  setup(__props, { expose: __expose, emit: __emit }) {

const props = __props;
const emit = __emit;
const state = ref$1({ requests: [], records: [] });
const draft = ref$1({
  enabled: false, host_coordinator: false, bot_username: '', group: 'resources',
  instance_label: '我的资源刷流', bot_token: '', allowed_users: '',
  transport: 'telegram', telegram_listener: true, https_listener: false,
  https_host: '', https_authority_id: '', https_token: '', https_peers: '',
});
const busy = ref$1(false);
const ready = ref$1(false);
const canTest = computed$1(() => Boolean(state.value.bot_username));
const transportOptions = [
  { value: 'telegram', title: 'Telegram Bot' },
  { value: 'https', title: 'Tailscale 私网 HTTPS' },
];
const error = ref$1('');
const recordRows = ref$1({ items: [], total: 0 });
const requestRows = computed$1(() => uniqueRequestRows(recordRows.value.items).map(row => ({
  ...row, mediaSummaries: requestMediaSummaries(row), rejectionSummaries: rejectionSummaries(row),
  claimSummary: claimLinkSummary(row),
})));
const recordPage = ref$1(1);
const recordsLoading = ref$1(false);
const recordFilters = ref$1({
  requests: { query: '', owner: '', media_type: '', status: '' },
  records: { query: '', owner: '', media_type: '', status: '' },
});
const activeFilter = computed$1(() => recordFilters.value[props.recordTab]);
const ownerOptions = computed$1(() => {
  const owners = new Set(recordRows.value.owners || []);
  if (activeFilter.value?.owner) owners.add(activeFilter.value.owner);
  return [
    { value: '', title: '全部下载方' },
    ...[...owners].filter(Boolean).sort((a, b) => a.localeCompare(b, 'zh-CN'))
      .map(value => ({ value, title: value })),
  ]
});
const mediaTypeOptions = [
  { value: '', title: '全部类型' },
  { value: 'tv', title: '剧集' },
  { value: 'movie', title: '电影' },
];
let searchTimer;
let recordsGeneration = 0;
const phaseNames = {
  requesting: '等待审批', reserved: '已获准', adding: '暂停添加中', awaiting_start: '等待入队确认',
  timeout_cancel_pending: '超时取消中',
  queued: '已入队', completed: '已完成', released: '已释放', rejected: '已过滤', uncertain: '待核验',
};
const phaseColors = {
  requesting: 'info', reserved: 'success', adding: 'info', awaiting_start: 'warning',
  queued: 'info', timeout_cancel_pending: 'warning', completed: 'success', released: 'secondary', rejected: 'error', uncertain: 'warning',
};
const phaseIcons = {
  requesting: 'mdi-clock-outline', reserved: 'mdi-check-circle-outline', adding: 'mdi-clock-outline',
  awaiting_start: 'mdi-clock-outline', timeout_cancel_pending: 'mdi-clock-alert-outline',
  queued: 'mdi-check-circle-outline', completed: 'mdi-check-circle-outline',
  released: 'mdi-minus-circle-outline', rejected: 'mdi-close-circle-outline', uncertain: 'mdi-help-circle-outline',
};
const statusOptions = computed$1(() => [
  { value: '', title: '全部状态' },
  ...(props.recordTab === 'requests'
    ? ['completed', 'queued', 'released', 'rejected']
    : ['reserved', 'queued', 'completed', 'released'])
    .map(value => ({ value, title: phaseNames[value] })),
]);

async function loadRecords() {
  if (props.mode !== 'records' || !activeFilter.value) return
  clearTimeout(searchTimer);
  const generation = ++recordsGeneration;
  recordsLoading.value = true;
  error.value = '';
  const params = new URLSearchParams({
    kind: props.recordTab, query: activeFilter.value.query || '', owner: activeFilter.value.owner || '',
    media_type: activeFilter.value.media_type || '', status: activeFilter.value.status || '',
    page: String(recordPage.value), page_size: '20',
  });
  try {
    const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/records?${params}`));
    if (generation !== recordsGeneration) return
    recordRows.value = data || { items: [], total: 0 };
    recordPage.value = data?.page || 1;
  } catch (failure) {
    if (generation === recordsGeneration) error.value = failure?.message || '读取共享记录失败';
  } finally {
    if (generation === recordsGeneration) recordsLoading.value = false;
  }
}

function timestamp(value) {
  if (!value) return '-'
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}

function requestSummary(row) {
  const approved = Number(row.approved || 0);
  const rejected = row.rejected?.length || 0;
  const completed = Number(row.completed || 0);
  if (row.phase === 'rejected') return `${phaseNames.rejected}（${rejected || row.file_count || 0}）`
  if (row.phase === 'queued' || row.phase === 'awaiting_start') return `已入队（${approved}）`
  if (row.phase === 'completed') return `已完成（${completed || approved}）`
  if (['reserved', 'adding'].includes(row.phase)) return `已批准（${approved}）`
  return phaseNames[row.phase] || row.phase
}

async function load(reset = false) {
  // Status and records are independent endpoints; start them together so the
  // first view does not wait for the status request before showing records.
  const recordsPromise = props.mode === 'records' ? loadRecords() : Promise.resolve();
  const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/status`));
  state.value = data;
  ready.value = true;
  if (props.mode === 'records') emit('host-change', Boolean(data.host_coordinator || data.enabled));
  if (reset) draft.value = {
    enabled: Boolean(data.enabled), host_coordinator: Boolean(data.host_coordinator),
    bot_username: data.bot_username || '', group: data.group || 'resources',
    instance_label: data.instance_label || '我的资源刷流', bot_token: '',
    allowed_users: (data.allowed_users || []).join(', '),
    transport: data.transport || 'telegram', telegram_listener: data.telegram_listener !== false,
    https_listener: Boolean(data.https_listener), https_host: data.https_host || '',
    https_authority_id: data.https_authority_id || '', https_token: '',
    https_peers: (data.https_peers || []).map(peer => `${peer.instance_id} ${peer.sender} ${peer.token_hash}`).join('\n'),
  };
  await recordsPromise;
}

async function perform(action) {
  busy.value = true;
  error.value = '';
  try { await action(); } catch (failure) { error.value = failure?.message || '共享审批操作失败'; }
  finally { busy.value = false; }
}

async function save() {
  busy.value = true;
  error.value = '';
  try {
    if (!ready.value) throw new Error('共享连接尚未加载，请关闭后重新打开全局设置')
    const users = draft.value.allowed_users.split(/[\s,，;；、]+/).filter(Boolean);
    if (users.some(value => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) {
      throw new Error('白名单须为个人账号数字 ID')
    }
    const payload = { ...draft.value, allowed_users: [...new Set(users.map(Number))] };
    if (!payload.bot_token) delete payload.bot_token;
    if (!payload.https_token) delete payload.https_token;
    payload.https_peers = draft.value.https_peers.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
      const [instance_id, rawSender, token_hash, extra] = line.split(/\s+/);
      const sender = Number(rawSender);
      if (extra || !/^[a-f0-9]{32}$/.test(instance_id) || !/^\d+$/.test(rawSender)
        || !Number.isSafeInteger(sender) || sender <= 0 || !/^[a-f0-9]{64}$/.test(token_hash)) {
        throw new Error('私网设备格式无效：每行填写实例 ID、个人账号数字 ID、密钥 SHA-256 指纹')
      }
      return { instance_id, sender, token_hash }
    });
    unwrapResponse(await props.api.post(`${props.pluginBase}/shared/settings`, payload));
    await load(true);
    return state.value
  } catch (failure) {
    error.value = failure?.message || '共享连接保存失败';
    throw failure
  } finally { busy.value = false; }
}

function test() {
  return perform(async () => {
    state.value = unwrapResponse(await props.api.post(`${props.pluginBase}/shared/test`, {}));
    emit('policy-synced');
  })
}

onMounted$1(() => perform(() => load(true)));
watch$1(() => draft.value.host_coordinator, value => {
  if (props.mode === 'settings') emit('host-change', value);
}, { immediate: true });
watch$1(recordsLoading, value => emit('loading-change', value), { immediate: true });
watch$1(() => [props.recordTab, activeFilter.value?.query, activeFilter.value?.owner,
  activeFilter.value?.media_type, activeFilter.value?.status], (values, previous) => {
  if (props.mode !== 'records') return
  clearTimeout(searchTimer);
  recordsGeneration += 1;
  // Keep the previous page visible while the next response is in flight.
  // Clearing it here caused the dialog to collapse and visibly jump on every
  // search, tab switch, and periodic refresh.
  recordsLoading.value = true;
  recordPage.value = 1;
  if (!activeFilter.value) return
  const delay = values[0] === previous[0] && values[2] === previous[2]
    && values[3] === previous[3] && values[4] === previous[4] ? 300 : 0;
  recordsLoading.value = true;
  searchTimer = setTimeout(loadRecords, delay);
});
onUnmounted$1(() => { clearTimeout(searchTimer); recordsGeneration += 1; });
__expose({ save, test, ready, busy, canTest, refresh: () => perform(() => load()) });

return (_ctx, _cache) => {
  const _component_VAlert = _resolveComponent$1("VAlert");
  const _component_VSwitch = _resolveComponent$1("VSwitch");
  const _component_VSelect = _resolveComponent$1("VSelect");
  const _component_VTextField = _resolveComponent$1("VTextField");
  const _component_VTextarea = _resolveComponent$1("VTextarea");
  const _component_VIcon = _resolveComponent$1("VIcon");

  return (_openBlock$1(), _createElementBlock$1("section", {
    class: _normalizeClass$1(["shared-settings", { 'shared-settings--compact': __props.mode === 'settings' }])
  }, [
    (error.value)
      ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
          key: 0,
          type: "error",
          variant: "tonal",
          density: "compact"
        }, {
          default: _withCtx$1(() => [
            _createTextVNode$1(_toDisplayString$1(error.value), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode$1("", true),
    (state.value.last_error)
      ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
          key: 1,
          type: "warning",
          variant: "tonal",
          density: "compact"
        }, {
          default: _withCtx$1(() => [
            _createTextVNode$1(_toDisplayString$1(state.value.last_error), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode$1("", true),
    (state.value.integrity_error)
      ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
          key: 2,
          type: "error",
          variant: "tonal",
          density: "compact"
        }, {
          default: _withCtx$1(() => [
            _createTextVNode$1(_toDisplayString$1(state.value.integrity_error), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode$1("", true),
    (state.value.history_sync?.pending)
      ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
          key: 3,
          type: "warning",
          variant: "tonal",
          density: "compact"
        }, {
          default: _withCtx$1(() => [
            _createTextVNode$1(" 历史完成证据待同步：" + _toDisplayString$1(state.value.history_sync.pending) + " 条；同步确认前暂缓新增下载。 ", 1)
          ]),
          _: 1
        }))
      : _createCommentVNode$1("", true),
    (__props.mode === 'settings')
      ? (_openBlock$1(), _createElementBlock$1(_Fragment$1, { key: 4 }, [
          (state.value.https_listener_error)
            ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
                key: 0,
                type: "warning",
                variant: "tonal",
                density: "compact"
              }, {
                default: _withCtx$1(() => [
                  _createTextVNode$1(_toDisplayString$1(state.value.https_listener_error), 1)
                ]),
                _: 1
              }))
            : _createCommentVNode$1("", true),
          _cache[23] || (_cache[23] = _createElementVNode$1("div", { class: "text-subtitle-2" }, "共享下载审批", -1)),
          _createElementVNode$1("div", _hoisted_1$1, [
            _createVNode$1(_component_VSwitch, {
              modelValue: draft.value.enabled,
              "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((draft.value.enabled) = $event)),
              label: "启用共享下载审批",
              color: "primary",
              density: "compact",
              "hide-details": "",
              inset: ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VSwitch, {
              modelValue: draft.value.host_coordinator,
              "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((draft.value.host_coordinator) = $event)),
              label: "在本机运行协调端",
              color: "primary",
              density: "compact",
              "hide-details": "",
              inset: ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VSelect, {
              modelValue: draft.value.transport,
              "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((draft.value.transport) = $event)),
              label: "审批传输",
              items: transportOptions,
              density: "comfortable",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VTextField, {
              modelValue: draft.value.group,
              "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((draft.value.group) = $event)),
              label: "共享组",
              density: "comfortable",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VTextField, {
              modelValue: draft.value.bot_username,
              "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((draft.value.bot_username) = $event)),
              label: "协调 Bot 用户名（现有账本保留原值）",
              placeholder: "@resource_bot",
              density: "comfortable",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VTextField, {
              modelValue: draft.value.instance_label,
              "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((draft.value.instance_label) = $event)),
              label: "本机名称",
              maxlength: "80",
              density: "comfortable",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            _createVNode$1(_component_VTextField, {
              "model-value": state.value.instance_id || '-',
              label: "本机实例 ID",
              class: "shared-settings__instance",
              readonly: "",
              density: "comfortable",
              "hide-details": ""
            }, null, 8, ["model-value"])
          ]),
          (draft.value.host_coordinator)
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_2$1, [
                _createVNode$1(_component_VSwitch, {
                  modelValue: draft.value.telegram_listener,
                  "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((draft.value.telegram_listener) = $event)),
                  label: "提供 Telegram 审批入口",
                  color: "primary",
                  density: "compact",
                  "hide-details": "",
                  inset: ""
                }, null, 8, ["modelValue"]),
                _createVNode$1(_component_VSwitch, {
                  modelValue: draft.value.https_listener,
                  "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((draft.value.https_listener) = $event)),
                  label: "提供私网 HTTPS 审批入口",
                  color: "primary",
                  density: "compact",
                  "hide-details": "",
                  inset: ""
                }, null, 8, ["modelValue"])
              ]))
            : _createCommentVNode$1("", true),
          (draft.value.host_coordinator && draft.value.telegram_listener)
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_3$1, [
                _createVNode$1(_component_VTextField, {
                  modelValue: draft.value.bot_token,
                  "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((draft.value.bot_token) = $event)),
                  type: "password",
                  autocomplete: "new-password",
                  label: state.value.token_configured ? '协调 Bot Token（已保存）' : '协调 Bot Token',
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue", "label"]),
                _createVNode$1(_component_VTextarea, {
                  modelValue: draft.value.allowed_users,
                  "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((draft.value.allowed_users) = $event)),
                  label: "允许的个人账号 ID",
                  rows: "1",
                  density: "comfortable",
                  placeholder: "123456789, 987654321; 112233445",
                  "auto-grow": "",
                  "hide-details": ""
                }, null, 8, ["modelValue"])
              ]))
            : _createCommentVNode$1("", true),
          (draft.value.host_coordinator && draft.value.https_listener)
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_4$1, [
                _createVNode$1(_component_VTextField, {
                  "model-value": state.value.coordinator_authority_id || '',
                  label: "共享账本 ID",
                  class: "shared-settings__instance",
                  readonly: "",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["model-value"]),
                _createVNode$1(_component_VTextarea, {
                  modelValue: draft.value.https_peers,
                  "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => ((draft.value.https_peers) = $event)),
                  label: "允许的私网设备（实例 ID / 个人账号 ID / 密钥指纹）",
                  rows: "2",
                  "max-rows": "6",
                  density: "comfortable",
                  "auto-grow": "",
                  "hide-details": ""
                }, null, 8, ["modelValue"])
              ]))
            : _createCommentVNode$1("", true),
          (draft.value.transport === 'https')
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_5$1, [
                (!draft.value.host_coordinator)
                  ? (_openBlock$1(), _createBlock$1(_component_VTextField, {
                      key: 0,
                      modelValue: draft.value.https_host,
                      "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((draft.value.https_host) = $event)),
                      label: "协调端 Tailscale HTTPS 域名",
                      placeholder: "approval.example.ts.net",
                      density: "comfortable",
                      "hide-details": ""
                    }, null, 8, ["modelValue"]))
                  : _createCommentVNode$1("", true),
                (!draft.value.host_coordinator)
                  ? (_openBlock$1(), _createBlock$1(_component_VTextField, {
                      key: 1,
                      modelValue: draft.value.https_authority_id,
                      "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((draft.value.https_authority_id) = $event)),
                      label: "协调端共享账本 ID",
                      class: "shared-settings__instance",
                      density: "comfortable",
                      "hide-details": ""
                    }, null, 8, ["modelValue"]))
                  : _createCommentVNode$1("", true),
                _createVNode$1(_component_VTextField, {
                  modelValue: draft.value.https_token,
                  "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((draft.value.https_token) = $event)),
                  type: "password",
                  autocomplete: "new-password",
                  label: state.value.https_token_configured ? '本机私网审批密钥（已保存）' : '本机私网审批密钥',
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue", "label"])
              ]))
            : _createCommentVNode$1("", true),
          _createElementVNode$1("dl", _hoisted_6$1, [
            _createElementVNode$1("div", null, [
              _cache[19] || (_cache[19] = _createElementVNode$1("dt", null, "审批传输：", -1)),
              _createElementVNode$1("dd", null, _toDisplayString$1(state.value.transport === 'https' ? 'Tailscale HTTPS' : 'Telegram（自动生成ED2K链接）'), 1)
            ]),
            _createElementVNode$1("div", null, [
              _cache[20] || (_cache[20] = _createElementVNode$1("dt", null, "协调端：", -1)),
              _createElementVNode$1("dd", null, _toDisplayString$1(state.value.host_coordinator ? state.value.coordinator_running ? '本机运行中' : '本机未运行' : '远端'), 1)
            ]),
            _createElementVNode$1("div", null, [
              _cache[21] || (_cache[21] = _createElementVNode$1("dt", null, "最近审批确认：", -1)),
              _createElementVNode$1("dd", null, _toDisplayString$1(timestamp(state.value.last_ok || state.value.coordinator_last_ok)), 1)
            ]),
            _createElementVNode$1("div", null, [
              _cache[22] || (_cache[22] = _createElementVNode$1("dt", null, "协调端规则版本：", -1)),
              _createElementVNode$1("dd", null, _toDisplayString$1(state.value.probe?.policy?.revision?.slice(0, 12) || '-'), 1)
            ])
          ])
        ], 64))
      : (_openBlock$1(), _createElementBlock$1(_Fragment$1, { key: 5 }, [
          (activeFilter.value)
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_7$1, [
                _createVNode$1(_component_VTextField, {
                  modelValue: activeFilter.value.query,
                  "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((activeFilter.value.query) = $event)),
                  label: "搜索记录",
                  "prepend-inner-icon": "mdi-magnify",
                  clearable: "",
                  maxlength: "200",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode$1(_component_VSelect, {
                  modelValue: activeFilter.value.owner,
                  "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((activeFilter.value.owner) = $event)),
                  label: "下载方筛选",
                  items: ownerOptions.value,
                  "item-title": "title",
                  "item-value": "value",
                  clearable: "",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue", "items"]),
                _createVNode$1(_component_VSelect, {
                  modelValue: activeFilter.value.media_type,
                  "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((activeFilter.value.media_type) = $event)),
                  label: "类型筛选",
                  items: mediaTypeOptions,
                  "item-title": "title",
                  "item-value": "value",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode$1(_component_VSelect, {
                  modelValue: activeFilter.value.status,
                  "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((activeFilter.value.status) = $event)),
                  label: "状态筛选",
                  items: statusOptions.value,
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue", "items"])
              ]))
            : _createCommentVNode$1("", true),
          (__props.recordTab === 'requests')
            ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_8$1, [
                (!recordsLoading.value && !recordRows.value.items.length)
                  ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_9$1, _toDisplayString$1(activeFilter.value.query || activeFilter.value.owner || activeFilter.value.media_type || activeFilter.value.status ? '无匹配记录' : '暂无共享申请'), 1))
                  : _createCommentVNode$1("", true),
                (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(requestRows.value, (row) => {
                  return (_openBlock$1(), _createElementBlock$1("article", {
                    key: row.request_id,
                    class: "shared-settings__record"
                  }, [
                    _createElementVNode$1("div", _hoisted_10$1, [
                      (row.mediaSummaries.length)
                        ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_11$1, [
                            (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(row.mediaSummaries, (summary, index) => {
                              return (_openBlock$1(), _createElementBlock$1("div", {
                                key: index,
                                class: "shared-settings__preview shared-settings__media-summary shared-settings__info text-body-2"
                              }, [
                                _createElementVNode$1("span", null, _toDisplayString$1(summary), 1)
                              ]))
                            }), 128))
                          ]))
                        : (_openBlock$1(), _createElementBlock$1("strong", _hoisted_12$1, _toDisplayString$1(row.title), 1)),
                      _createElementVNode$1("span", {
                        class: "shared-settings__badge",
                        "data-tone": phaseColors[row.phase] || 'secondary'
                      }, [
                        _createVNode$1(_component_VIcon, {
                          icon: phaseIcons[row.phase] || 'mdi-clock-outline',
                          size: "16"
                        }, null, 8, ["icon"]),
                        _createTextVNode$1(" " + _toDisplayString$1(requestSummary(row)), 1)
                      ], 8, _hoisted_13$1)
                    ]),
                    (row.mediaSummaries.length)
                      ? (_openBlock$1(), _createElementBlock$1("strong", _hoisted_14$1, _toDisplayString$1(row.title), 1))
                      : _createCommentVNode$1("", true),
                    _createElementVNode$1("div", _hoisted_15$1, [
                      _createElementVNode$1("span", null, "申请时间：" + _toDisplayString$1(timestamp(row.created)), 1),
                      _createElementVNode$1("span", null, "最近更新：" + _toDisplayString$1(timestamp(row.updated)), 1),
                      (row.download_started_at)
                        ? (_openBlock$1(), _createElementBlock$1("span", _hoisted_16$1, "下载开始：" + _toDisplayString$1(timestamp(row.download_started_at)), 1))
                        : _createCommentVNode$1("", true),
                      (row.timeout_at)
                        ? (_openBlock$1(), _createElementBlock$1("span", _hoisted_17$1, "超时处理：" + _toDisplayString$1(timestamp(row.timeout_at)), 1))
                        : _createCommentVNode$1("", true),
                      _createElementVNode$1("span", null, "任务：" + _toDisplayString$1(row.task_name || row.task_id || '-'), 1),
                      _createElementVNode$1("span", null, "下载器：" + _toDisplayString$1(row.downloader || '-'), 1)
                    ]),
                    (row.error && row.phase !== 'rejected')
                      ? (_openBlock$1(), _createElementBlock$1("p", _hoisted_18$1, _toDisplayString$1(row.error), 1))
                      : _createCommentVNode$1("", true),
                    (row.integrity_error)
                      ? (_openBlock$1(), _createElementBlock$1("p", _hoisted_19$1, _toDisplayString$1(row.integrity_error), 1))
                      : _createCommentVNode$1("", true),
                    (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(row.rejectionSummaries, (reason, index) => {
                      return (_openBlock$1(), _createElementBlock$1("p", {
                        key: index,
                        class: "shared-settings__reason text-body-2"
                      }, _toDisplayString$1(reason), 1))
                    }), 128)),
                    _createElementVNode$1("div", _hoisted_20$1, [
                      _createElementVNode$1("span", _hoisted_21$1, "申请编号：" + _toDisplayString$1(row.request_id), 1),
                      (row.claimSummary)
                        ? (_openBlock$1(), _createElementBlock$1("span", _hoisted_22$1, " 关联占位：" + _toDisplayString$1(row.claimSummary), 1))
                        : _createCommentVNode$1("", true)
                    ])
                  ]))
                }), 128))
              ]))
            : (__props.recordTab === 'records' && (state.value.host_coordinator || state.value.enabled))
              ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_23$1, [
                  (!recordsLoading.value && !recordRows.value.items.length)
                    ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_24$1, _toDisplayString$1(activeFilter.value.query || activeFilter.value.owner || activeFilter.value.media_type || activeFilter.value.status ? '无匹配记录' : '暂无共享占位'), 1))
                    : _createCommentVNode$1("", true),
                  (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(recordRows.value.items, (row) => {
                    return (_openBlock$1(), _createElementBlock$1("article", {
                      key: row.id,
                      class: "shared-settings__record"
                    }, [
                      _createElementVNode$1("div", _hoisted_25$1, [
                        _createElementVNode$1("div", _hoisted_26$1, [
                          _createElementVNode$1("div", _hoisted_27$1, [
                            _createElementVNode$1("span", null, _toDisplayString$1(_unref$1(releaseDetails)(row)), 1)
                          ])
                        ]),
                        _createElementVNode$1("span", {
                          class: "shared-settings__badge",
                          "data-tone": phaseColors[row.state] || 'secondary'
                        }, [
                          _createVNode$1(_component_VIcon, {
                            icon: phaseIcons[row.state] || 'mdi-clock-outline',
                            size: "16"
                          }, null, 8, ["icon"]),
                          _createTextVNode$1(" " + _toDisplayString$1(phaseNames[row.state] || row.state), 1)
                        ], 8, _hoisted_28$1)
                      ]),
                      _createElementVNode$1("strong", _hoisted_29$1, _toDisplayString$1(row.title), 1),
                      _createElementVNode$1("div", _hoisted_30$1, [
                        _createElementVNode$1("span", null, "下载时间：" + _toDisplayString$1(timestamp(row.created)), 1),
                        _createElementVNode$1("span", null, "最近更新：" + _toDisplayString$1(timestamp(row.updated)), 1),
                        _createElementVNode$1("span", _hoisted_31$1, "下载方：" + _toDisplayString$1(row.label || '-'), 1)
                      ]),
                      _createElementVNode$1("div", _hoisted_32$1, [
                        _createElementVNode$1("span", _hoisted_33$1, "占位编号：" + _toDisplayString$1(row.id), 1),
                        (row.request_id)
                          ? (_openBlock$1(), _createElementBlock$1("span", _hoisted_34$1, "关联申请：" + _toDisplayString$1(row.request_id), 1))
                          : _createCommentVNode$1("", true)
                      ])
                    ]))
                  }), 128))
                ]))
              : _createCommentVNode$1("", true),
          (activeFilter.value)
            ? (_openBlock$1(), _createBlock$1(SharedRecordPagination, {
                key: 3,
                modelValue: recordPage.value,
                "onUpdate:modelValue": [
                  _cache[18] || (_cache[18] = $event => ((recordPage).value = $event)),
                  loadRecords
                ],
                total: recordRows.value.total
              }, null, 8, ["modelValue", "total"]))
            : _createCommentVNode$1("", true)
        ], 64))
  ], 2))
}
}

};
const SharedSettingsPanel = /*#__PURE__*/_export_sfc(_sfc_main$1, [['__scopeId',"data-v-69843f49"]]);

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,toDisplayString:_toDisplayString,createTextVNode:_createTextVNode,withCtx:_withCtx,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,mergeProps:_mergeProps,createElementBlock:_createElementBlock,unref:_unref,renderList:_renderList,Fragment:_Fragment,normalizeClass:_normalizeClass,normalizeStyle:_normalizeStyle,withModifiers:_withModifiers} = await importShared('vue');


const _hoisted_1 = { class: "resourceflow-page__header" };
const _hoisted_2 = { class: "resourceflow-page__identity" };
const _hoisted_3 = { class: "resourceflow-page__actions" };
const _hoisted_4 = {
  key: 2,
  class: "resourceflow-loading"
};
const _hoisted_5 = {
  key: 3,
  class: "resourceflow-empty"
};
const _hoisted_6 = { class: "resourceflow-layout" };
const _hoisted_7 = { class: "resourceflow-task-rail__head" };
const _hoisted_8 = { class: "resourceflow-task-list" };
const _hoisted_9 = ["aria-pressed", "onClick"];
const _hoisted_10 = { class: "resourceflow-task-item__title" };
const _hoisted_11 = { class: "resourceflow-task-item__meta" };
const _hoisted_12 = {
  key: 0,
  class: "resourceflow-workspace"
};
const _hoisted_13 = { class: "resourceflow-task-head" };
const _hoisted_14 = { class: "resourceflow-task-head__identity" };
const _hoisted_15 = { class: "resourceflow-task-head__title" };
const _hoisted_16 = { class: "resourceflow-task-head__actions" };
const _hoisted_17 = { class: "resourceflow-stat-grid" };
const _hoisted_18 = { key: 0 };
const _hoisted_19 = { key: 1 };
const _hoisted_20 = { class: "resourceflow-overview-grid" };
const _hoisted_21 = { class: "resourceflow-panel__head" };
const _hoisted_22 = { class: "resourceflow-facts" };
const _hoisted_23 = { key: 0 };
const _hoisted_24 = { class: "resourceflow-panel__head" };
const _hoisted_25 = { class: "text-body-2 text-medium-emphasis" };
const _hoisted_26 = { class: "resourceflow-run-summary" };
const _hoisted_27 = { class: "resourceflow-panel__head" };
const _hoisted_28 = { class: "resourceflow-panel__title-row" };
const _hoisted_29 = { class: "torrent-title-cell" };
const _hoisted_30 = { class: "resourceflow-mobile-torrents" };
const _hoisted_31 = { class: "resourceflow-mobile-torrent__head" };
const _hoisted_32 = { class: "resourceflow-mobile-torrent__meta" };
const _hoisted_33 = { class: "text-body-2 text-medium-emphasis" };
const _hoisted_34 = {
  key: 0,
  class: "resourceflow-table-empty"
};
const _hoisted_35 = { class: "resourceflow-diagnostic-head" };
const _hoisted_36 = { class: "text-subtitle-1 font-weight-medium" };
const _hoisted_37 = { class: "text-body-2 text-medium-emphasis" };
const _hoisted_38 = { class: "resourceflow-diagnostic-grid" };
const _hoisted_39 = { class: "resourceflow-pipeline" };
const _hoisted_40 = { class: "resourceflow-pipeline__index" };
const _hoisted_41 = {
  key: 0,
  class: "resourceflow-reasons"
};
const _hoisted_42 = { class: "resourceflow-reason__track" };
const _hoisted_43 = {
  key: 1,
  class: "resourceflow-table-empty"
};
const _hoisted_44 = { class: "resourceflow-events" };
const _hoisted_45 = {
  key: 0,
  class: "text-error"
};
const _hoisted_46 = {
  key: 0,
  class: "resourceflow-table-empty"
};
const _hoisted_47 = { class: "resourceflow-config-grid" };
const _hoisted_48 = { class: "resourceflow-facts resourceflow-facts--two" };
const _hoisted_49 = { class: "resourceflow-config-actions" };
const _hoisted_50 = { class: "global-settings__progress" };
const _hoisted_51 = { class: "channel-dedup__section" };
const _hoisted_52 = { class: "global-settings__toggles" };
const _hoisted_53 = {
  key: 0,
  class: "global-settings__grid"
};
const _hoisted_54 = { class: "global-settings__grid global-settings__limits" };
const _hoisted_55 = { class: "global-settings__grid global-settings__recent-date" };
const _hoisted_56 = { class: "channel-dedup__section" };
const _hoisted_57 = { class: "global-settings__spec-row" };
const _hoisted_58 = { class: "global-settings__attributes" };
const _hoisted_59 = {
  key: 0,
  class: "global-settings__grid"
};
const _hoisted_60 = { class: "global-settings__grid global-settings__aliases" };
const _hoisted_61 = { class: "global-settings__footer" };
const _hoisted_62 = { class: "global-settings__footer-group" };
const _hoisted_63 = { class: "global-settings__footer-group" };
const _hoisted_64 = {
  key: 3,
  class: "channel-dedup__section"
};
const _hoisted_65 = { class: "channel-dedup__search" };
const _hoisted_66 = {
  key: 0,
  class: "text-body-2 text-medium-emphasis"
};
const _hoisted_67 = { class: "channel-dedup__records" };
const _hoisted_68 = { class: "channel-dedup__record-head" };
const _hoisted_69 = { class: "channel-dedup__record-main" };
const _hoisted_70 = { class: "channel-dedup__record-identity channel-dedup__record-info" };
const _hoisted_71 = { class: "channel-dedup__record-actions" };
const _hoisted_72 = { class: "channel-dedup__record-title" };
const _hoisted_73 = { class: "channel-dedup__record-meta" };
const _hoisted_74 = { key: 0 };
const _hoisted_75 = { class: "channel-dedup__record-reason" };
const _hoisted_76 = {
  key: 0,
  class: "channel-dedup__record-progress"
};
const _hoisted_77 = {
  key: 1,
  class: "channel-dedup__record-error"
};
const _hoisted_78 = {
  key: 2,
  class: "channel-dedup__record-error"
};
const _hoisted_79 = { class: "pending-approval__grid" };
const _hoisted_80 = {
  key: 0,
  class: "pending-approval__lookup"
};
const _hoisted_81 = {
  key: 0,
  role: "status",
  class: "text-body-2 text-medium-emphasis"
};
const _hoisted_82 = {
  key: 1,
  class: "pending-approval__filename text-caption text-medium-emphasis"
};

const {computed,inject,onMounted,onUnmounted,ref,watch} = await importShared('vue');


const _sfc_main = {
  __name: 'ResourceFlowWorkbench',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'ResourceFlow' },
  initialTab: { type: String, default: 'overview' },
  showClose: { type: Boolean, default: false },
  compact: { type: Boolean, default: false },
},
  emits: ['close', 'action'],
  setup(__props, { expose: __expose, emit: __emit }) {

const props = __props;

const emit = __emit;
const loading = ref(true);
const taskLoading = ref(false);
const saving = ref(false);
const error = ref('');
// 首次状态请求完成前，不把默认的停用状态展示给用户。
const statusLoaded = ref(false);
const status = ref({
  enabled: false,
  show_sidebar_nav: true,
  summary: {},
  tasks: [],
  options: { sites: [], downloaders: [] },
});
const taskDetail = ref(null);
const selectedTaskId = ref('');
const activeTab = ref(props.initialTab);
const torrentState = ref('active');
const torrentPage = ref(1);
const editorOpen = ref(false);
const editorTask = ref(cloneTask());
const deleteDialog = ref(false);
const clearDialog = ref(false);
const settingsDialog = ref(false);
const settingsError = ref('');
const settingsReady = ref(false);
const channelDedupDialog = ref(false);
const channelDedupLoading = ref(false);
const channelDedupError = ref('');
const sharedSettingsPanel = ref(null);
const sharedRecordsPanel = ref(null);
const sharedHost = ref(false);
const sharedRecordsHost = ref(false);
const sharedRecordsLoading = ref(false);
const approvalTab = ref('requests');
const resourceRows = ref({ total: 0, items: [] });
const resourcePage = ref(1);
const activeResourceFilter = ref({ query: '', media_type: '' });
const pendingApprovalDialog = ref(false);
const pendingApprovalSaving = ref(false);
const pendingApprovalRow = ref(null);
const pendingApprovalError = ref('');
const pendingSearchOpen = ref(false);
const pendingSearchQuery = ref('');
const pendingSearchResults = ref([]);
const pendingSearchLoading = ref(false);
const pendingSearchMessage = ref('');
let pendingSearchVersion = 0;
const pendingApprovalForm = ref({
  tmdb_id: '', media_title: '', media_type: 'tv', season: 1, episodes: '', specs: {}, confirmed: false,
});
const resourceTypes = [
  { value: '', title: '全部类型' }, { value: 'tv', title: '剧集' }, { value: 'movie', title: '电影' },
];
let resourceSearchTimer;
let resourceGeneration = 0;
const ledgerLoading = ref(false);
const approvalLoading = computed(() => (
  channelDedupLoading.value || sharedRecordsLoading.value || ledgerLoading.value
));
const channelDedup = ref({ enabled: false, channels: [], attributes: [], records: [] });
const channelDedupDraft = ref({
  enabled: false, channels: '', attributes: [], size_tolerance_pct: 5,
  unknown_policy: 'exclude', identity_aliases: '', cancel_downloading: false,
  movie_identity_aliases: '',
});
const channelUnknownPolicies = [
  { value: 'exclude', title: '排除不下载' },
  { value: 'allow', title: '按未命中放行（兼容旧版）' },
];
const channelDedupAttributes = [
  { value: 'resolution', title: '分辨率' },
  { value: 'effects', title: '特效' },
  { value: 'codec', title: '编码' },
  { value: 'source', title: '来源' },
  { value: 'fps', title: '帧率' },
  { value: 'size', title: '大小' },
];
const settingsDraft = ref({
  enabled: false,
  show_sidebar_nav: true,
  global_disksize: null,
  global_maxdlcount: null,
  global_maxupspeed: null,
  global_maxdlspeed: null,
  global_proxy_delete: false,
  global_delete_size_range: null,
  recent_missing_date_tolerance_days: 7,
  recent_missing_date_ai_enabled: false,
});
const hostToast = inject('moviepilot:toast', null);
let refreshTimer;

const pluginBase = computed(() => `plugin/${props.pluginId || 'ResourceFlow'}`);
const tasks = computed(() => status.value.tasks || []);
const selectedTask = computed(() => tasks.value.find(item => item.id === selectedTaskId.value) || null);
const selectedState = computed(() => taskStateMeta(selectedTask.value?.state));
const taskConfig = computed(() => taskDetail.value?.task || {});
const taskRuns = computed(() => taskDetail.value?.runs || []);
const latestBrushRun = computed(() => taskRuns.value.find(item => item.kind === 'brush') || null);
const torrentData = computed(() => taskDetail.value?.torrents || { items: [], total: 0, page: 1, page_size: 50 });
const totalTorrentPages = computed(() => Math.max(Math.ceil(torrentData.value.total / torrentData.value.page_size), 1));
const seedingPercent = computed(() => {
  const limit = Number(taskConfig.value.disksize || 0) * 1024 ** 3;
  if (!limit) return 0
  return Math.min(Math.round((Number(selectedTask.value?.seeding_size || 0) * 100) / limit), 100)
});
const reasonEntries = computed(() => {
  const reasons = latestBrushRun.value?.reason_counts || {};
  return Object.entries(reasons)
    .map(([label, count]) => ({ label, count: Number(count || 0) }))
    .sort((left, right) => right.count - left.count)
});
const maxReasonCount = computed(() => Math.max(1, ...reasonEntries.value.map(item => item.count)));
const pipelineStages = computed(() => {
  const run = latestBrushRun.value || {};
  const sourceCount = Number(run.source_count || 0);
  const candidateCount = Number(run.candidate_count || 0);
  const addedCount = Number(run.added_count || 0);
  return [
    { title: '获取站点种子', detail: taskConfig.value.rss_support ? 'RSS' : '站点列表页', count: sourceCount },
    { title: '排除订阅内容', detail: `排除 ${Number(run.subscription_excluded || 0)} 个`, count: candidateCount },
    { title: '选种条件过滤', detail: `过滤 ${Number(run.filtered_count || 0)} 个`, count: addedCount },
    { title: '添加下载任务', detail: run.success === false ? '执行失败' : '下载器已确认', count: addedCount },
  ]
});

const torrentHeaders = [
  { title: '种子', key: 'title', sortable: false },
  { title: '状态', key: 'status', sortable: false },
  { title: '大小', key: 'size', align: 'end', sortable: false },
  { title: '上传', key: 'uploaded', align: 'end', sortable: false },
  { title: '分享率', key: 'ratio', align: 'end', sortable: false },
  { title: '处理条件', key: 'policy', sortable: false },
];

// 通过宿主注入的 Toast 显示操作结果，不在插件内创建第二套通知层。
function notify(text, color = 'success') {
  const method = ['error', 'info', 'warning', 'success'].includes(color) ? color : 'success';
  if (typeof hostToast?.[method] === 'function') {
    hostToast[method](text);
  } else if (method === 'error') {
    error.value = text;
  }
}

// 从插件状态接口加载全局设置和任务摘要。
async function loadStatus({ preserveSelection = true, loadDetail = true, resetDraft = false } = {}) {
  loading.value = true;
  error.value = '';
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/status`));
    status.value = data || status.value;
    statusLoaded.value = true;
    if (!settingsDialog.value || resetDraft) settingsDraft.value = {
      enabled: Boolean(status.value.enabled),
      show_sidebar_nav: status.value.show_sidebar_nav !== false,
      global_disksize: status.value.global_disksize ?? null,
      global_maxdlcount: status.value.global_maxdlcount ?? null,
      global_maxupspeed: status.value.global_maxupspeed ?? null,
      global_maxdlspeed: status.value.global_maxdlspeed ?? null,
      global_proxy_delete: Boolean(status.value.global_proxy_delete),
      global_delete_size_range: status.value.global_delete_size_range ?? null,
      recent_missing_date_tolerance_days: Number(status.value.recent_missing_date_tolerance_days || 7),
      recent_missing_date_ai_enabled: Boolean(status.value.recent_missing_date_ai_enabled),
    };
    const selectedStillExists = tasks.value.some(item => item.id === selectedTaskId.value);
    if (!preserveSelection || !selectedStillExists) selectedTaskId.value = tasks.value[0]?.id || '';
    if (loadDetail && selectedTaskId.value) await loadTaskDetail();
  } catch (err) {
    error.value = err?.message || '加载刷流任务失败';
  } finally {
    loading.value = false;
  }
}

// 加载选中任务的配置、诊断记录和当前分页种子。
async function loadTaskDetail() {
  if (!selectedTaskId.value) {
    taskDetail.value = null;
    return
  }
  taskLoading.value = true;
  try {
    const query = `state=${torrentState.value}&page=${torrentPage.value}&page_size=50`;
    taskDetail.value = unwrapResponse(
      await props.api.get(`${pluginBase.value}/tasks/${selectedTaskId.value}?${query}`),
    );
  } catch (err) {
    error.value = err?.message || '加载任务详情失败';
  } finally {
    taskLoading.value = false;
  }
}

// 切换左侧任务并从第一页加载对应明细。
async function selectTask(taskId) {
  if (!taskId || taskId === selectedTaskId.value) return
  selectedTaskId.value = taskId;
  torrentState.value = 'active';
  torrentPage.value = 1;
  await loadTaskDetail();
}

// 同时刷新任务摘要和当前任务详情。
async function refreshAll(showMessage = false) {
  await loadStatus();
  if (showMessage) notify('刷流任务数据已刷新');
  emit('action');
}

// 打开一个空白任务草稿。
function openCreateTask() {
  editorTask.value = cloneTask();
  editorOpen.value = true;
}

// 打开当前任务的服务端配置快照。
function openEditTask() {
  if (!taskConfig.value.id) return
  editorTask.value = cloneTask(taskConfig.value);
  editorOpen.value = true;
}

// 通过任务 API 创建或更新任务并刷新调度状态。
async function saveTask(task) {
  saving.value = true;
  try {
    const response = task.id
      ? await props.api.put(`${pluginBase.value}/tasks/${task.id}`, task)
      : await props.api.post(`${pluginBase.value}/tasks`, task);
    const detail = unwrapResponse(response);
    editorOpen.value = false;
    selectedTaskId.value = detail?.task?.id || task.id || selectedTaskId.value;
    await loadStatus();
    notify(task.id ? '刷流任务已更新' : '刷流任务已创建');
  } catch (err) {
    notify(err?.message || '保存刷流任务失败', 'error');
  } finally {
    saving.value = false;
  }
}

// 切换当前任务启停状态并立即重建宿主调度。
async function toggleSelectedTask() {
  if (!selectedTask.value) return
  saving.value = true;
  try {
    unwrapResponse(
      await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/state`, {
        enabled: !selectedTask.value.enabled,
      }),
    );
    await loadStatus();
    notify(selectedTask.value?.enabled ? '刷流任务已启用' : '刷流任务已暂停');
  } catch (err) {
    notify(err?.message || '更新任务状态失败', 'error');
  } finally {
    saving.value = false;
  }
}

// 提交一次刷流刷新或种子检查操作。
async function runOperation(operation) {
  if (!selectedTaskId.value) return
  saving.value = true;
  try {
    const path = operation === 'brush' ? 'run' : 'check';
    unwrapResponse(await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/${path}`, {}));
    await loadStatus();
    notify(operation === 'brush' ? '刷流刷新已提交' : '种子检查已提交');
  } catch (err) {
    notify(err?.message || '提交任务失败', 'error');
  } finally {
    saving.value = false;
  }
}

async function openSettings() {
  settingsError.value = '';
  settingsReady.value = false;
  settingsDialog.value = true;
  await loadStatus({ loadDetail: false, resetDraft: true });
  const loaded = await loadChannelDedup();
  settingsReady.value = loaded && !error.value;
}

// 依次保存共享连接、协调端规则及原全局设置；失败时保留弹窗。
async function saveSettings() {
  saving.value = true;
  settingsError.value = '';
  try {
    if (!settingsReady.value || !sharedSettingsPanel.value) throw new Error('全局设置尚未加载，请关闭后重试')
    const payload = normalizeSettings(settingsDraft.value);
    const shared = await sharedSettingsPanel.value.save();
    await saveChannelDedup(shared);
    status.value = unwrapResponse(await props.api.post(`${pluginBase.value}/settings`, payload));
    await loadStatus({ resetDraft: true });
    notify('全局设置已保存');
  } catch (err) {
    settingsError.value = `全局设置未全部保存：${err?.message || '保存失败'}`;
  } finally {
    saving.value = false;
  }
}

async function loadChannelDedup({ resetDraft = true } = {}) {
  channelDedupLoading.value = true;
  channelDedupError.value = '';
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/channel-dedup`));
    channelDedup.value = data || channelDedup.value;
    if (resetDraft) {
      channelDedupDraft.value = {
        enabled: Boolean(data?.enabled),
        cancel_downloading: Boolean(data?.cancel_downloading),
        channels: (data?.channels || []).join('\n'),
        attributes: [...(data?.attributes || [])],
        size_tolerance_pct: Number(data?.size_tolerance_pct ?? 5),
        unknown_policy: data?.unknown_policy || 'exclude',
        identity_aliases: Object.entries(data?.identity_aliases || {}).map(([name, id]) => `${name} = ${id}`).join('\n'),
        movie_identity_aliases: Object.entries(data?.movie_identity_aliases || {}).map(([name, id]) => `${name} = ${id}`).join('\n'),
      };
    }
    return true
  } catch (err) {
    channelDedupError.value = err?.message || '读取资源去重设置失败';
    return false
  } finally {
    channelDedupLoading.value = false;
  }
}

async function openChannelDedup() {
  approvalTab.value = 'requests';
  sharedRecordsHost.value = false;
  channelDedupDialog.value = true;
  await Promise.all([
    loadChannelDedup(),
    approvalTab.value === 'pending' ? loadResourceRows() : Promise.resolve(),
  ]);
}

async function loadResourceRows() {
  clearTimeout(resourceSearchTimer);
  const generation = ++resourceGeneration;
  ledgerLoading.value = true;
  const params = new URLSearchParams({
    state: 'pending', page: String(resourcePage.value), page_size: '20',
    query: activeResourceFilter.value.query || '', media_type: activeResourceFilter.value.media_type || '',
  });
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/resource-ledger?${params}`));
    if (generation === resourceGeneration) resourceRows.value = data || { total: 0, items: [] };
  } catch (err) {
    if (generation === resourceGeneration) channelDedupError.value = err?.message || '读取下载待核验记录失败';
  } finally {
    if (generation === resourceGeneration) ledgerLoading.value = false;
  }
}

watch(() => [activeResourceFilter.value.query, activeResourceFilter.value.media_type],
  (values, previous) => {
    clearTimeout(resourceSearchTimer);
    resourceGeneration += 1;
    resourcePage.value = 1;
    // Keep the previous records visible while the filtered result is loading.
    ledgerLoading.value = true;
    const delay = values[1] === previous[1] ? 300 : 0;
    resourceSearchTimer = setTimeout(loadResourceRows, delay);
  });
watch(approvalTab, value => {
  if (value === 'pending') loadResourceRows();
});

async function refreshApprovalRecords() {
  await Promise.all([
    sharedRecordsPanel.value?.refresh(),
    loadChannelDedup({ resetDraft: false }),
    approvalTab.value === 'pending' ? loadResourceRows() : Promise.resolve(),
  ]);
}

async function saveChannelDedup(shared) {
  const data = unwrapResponse(await props.api.post(`${pluginBase.value}/channel-dedup/settings`, {
    enabled: false,
    cancel_downloading: false,
    ...(shared.host_coordinator ? {
      attributes: channelDedupDraft.value.attributes,
      size_tolerance_pct: channelDedupDraft.value.size_tolerance_pct,
      unknown_policy: channelDedupDraft.value.unknown_policy,
    } : {}),
    channels: '',
    identity_aliases: channelDedupDraft.value.identity_aliases,
    movie_identity_aliases: channelDedupDraft.value.movie_identity_aliases,
  }));
  channelDedup.value = data || channelDedup.value;
}

function formatChannelTimestamp(value) {
  if (!value) return '尚未同步'
  return new Date(Number(value) * 1000).toLocaleString()
}

function openPendingApproval(row) {
  pendingApprovalRow.value = row;
  pendingApprovalError.value = '';
  pendingApprovalForm.value = {
    tmdb_id: row.tmdb_id || '',
    media_title: row.media_title || '',
    media_type: row.media_type === 'movie' ? 'movie' : 'tv',
    season: row.source_season && row.source_season === row.season ? '' : (row.season ?? 1),
    episodes: (row.episodes || []).join(','),
    specs: Object.fromEntries(channelDedup.value.attributes.map(key => [key, row.specs?.[key] ?? ''])),
    confirmed: false,
  };
  pendingApprovalDialog.value = true;
}

watch(pendingApprovalDialog, (open) => {
  if (!open) {
    pendingSearchVersion += 1;
    pendingSearchOpen.value = false;
    pendingSearchLoading.value = false;
    pendingSearchResults.value = [];
    pendingSearchMessage.value = '';
  }
});

function togglePendingSearch() {
  pendingSearchOpen.value = !pendingSearchOpen.value;
  if (!pendingSearchOpen.value) {
    pendingSearchVersion += 1;
    pendingSearchLoading.value = false;
    return
  }
  pendingSearchQuery.value = pendingApprovalForm.value.media_title;
  searchPendingMedia();
}

async function searchPendingMedia() {
  const version = ++pendingSearchVersion;
  const title = (pendingSearchQuery.value || '').trim();
  pendingSearchResults.value = [];
  pendingSearchMessage.value = '';
  pendingSearchLoading.value = Boolean(title);
  if (!title) return
  try {
    const params = new URLSearchParams({ title, type: 'media', media_source: 'themoviedb', count: '20' });
    const rows = unwrapResponse(await props.api.get(`media/search?${params}`));
    if (version !== pendingSearchVersion) return
    const unique = new Map();
    for (const row of Array.isArray(rows) ? rows : []) {
      const type = { 电视剧: 'tv', 剧集: 'tv', tv: 'tv', 电影: 'movie', movie: 'movie' }[row?.type];
      const id = Number(row?.tmdb_id);
      if (!type || !Number.isSafeInteger(id) || id <= 0 || !row.title) continue
      unique.set(`${type}:${id}`, { id, type, title: row.title, year: row.year });
    }
    pendingSearchResults.value = [...unique.values()];
    if (!unique.size) pendingSearchMessage.value = '未找到匹配的 TMDB 媒体';
  } catch (err) {
    if (version === pendingSearchVersion) pendingSearchMessage.value = err?.message || '媒体搜索失败';
  } finally {
    if (version === pendingSearchVersion) pendingSearchLoading.value = false;
  }
}

async function selectPendingMedia(row) {
  const form = pendingApprovalForm.value;
  Object.assign(form, { tmdb_id: row.id, media_title: row.title, media_type: row.type, confirmed: false });
  pendingSearchOpen.value = false;
  const version = ++pendingSearchVersion;
  pendingSearchLoading.value = false;
  const pending = pendingApprovalRow.value;
  const year = pending?.source_season;
  if (row.type !== 'tv' || !year) return
  if (Number(pending.tmdb_id) === row.id && Number.isInteger(pending.season) && pending.season !== year) {
    form.season = pending.season;
    return
  }
  form.season = '';
  pendingSearchLoading.value = true;
  try {
    const params = new URLSearchParams({ media_source: 'themoviedb', media_id: String(row.id) });
    const seasons = unwrapResponse(await props.api.get(`media/seasons?${params}`));
    if (version !== pendingSearchVersion || form.season !== '' || Number(form.tmdb_id) !== row.id
        || form.media_type !== 'tv') return
    const candidates = new Set((Array.isArray(seasons) ? seasons : [])
      .filter(item => [`${year}`, `${year}年`].includes(String(item?.name || '').trim())
        && Number.isInteger(item.season_number) && item.season_number > 0 && item.season_number <= 999)
      .map(item => item.season_number));
    if (candidates.size === 1) form.season = [...candidates][0];
  } catch {
    // Keep the standard season editable if the lookup is unavailable.
  } finally {
    if (version === pendingSearchVersion) pendingSearchLoading.value = false;
  }
}

function parseManualEpisodes(value) {
  const episodes = new Set();
  for (const part of String(value || '').split(/[，,\s]+/).filter(Boolean)) {
    const range = part.match(/^(\d+)\s*[-~至]\s*(\d+)$/);
    if (range) {
      const first = Number(range[1]);
      const last = Number(range[2]);
      if (last < first || last - first > 500) return null
      for (let number = first; number <= last; number += 1) episodes.add(number);
      continue
    }
    if (!/^\d+$/.test(part)) return null
    episodes.add(Number(part));
  }
  return [...episodes].sort((a, b) => a - b)
}

async function submitPendingApproval() {
  const row = pendingApprovalRow.value;
  if (!row || pendingApprovalSaving.value) return
  const form = pendingApprovalForm.value;
  const episodes = parseManualEpisodes(form.episodes);
  const id = Number(form.tmdb_id);
  const season = Number(form.season);
  if (!Number.isSafeInteger(id) || id <= 0 || !form.media_title.trim() || !form.confirmed
    || (form.media_type === 'tv' && (String(form.season ?? '').trim() === ''
      || !Number.isInteger(season) || season < 0 || season > 999 || !episodes?.length))) {
    pendingApprovalError.value = '请填写 TMDB ID、媒体名称及有效的季集号。';
    return
  }
  pendingApprovalSaving.value = true;
  pendingApprovalError.value = '';
  try {
    unwrapResponse(await props.api.post(`${pluginBase.value}/resource-ledger/pending/approve`, {
      key: row.key,
      tmdb_id: id,
      media_title: form.media_title.trim(),
      media_type: form.media_type,
      season: form.media_type === 'tv' ? season : null,
      episodes: form.media_type === 'tv' ? episodes : [],
      specs: form.specs,
      confirmed: form.confirmed,
    }));
    pendingApprovalDialog.value = false;
    await loadResourceRows();
    const poll = async remaining => {
      if (remaining <= 0 || approvalTab.value !== 'pending') return
      await new Promise(resolve => setTimeout(resolve, 2000));
      await loadResourceRows();
      const current = resourceRows.value.items.find(item => item.key === row.key);
      if (current?.manual_state === 'queued' || current?.manual_state === 'processing') {
        poll(remaining - 1);
      }
    };
    poll(30);
  } catch (err) {
    pendingApprovalError.value = err?.message || '提交人工核验失败';
  } finally {
    pendingApprovalSaving.value = false;
  }
}

function channelRecordEpisodes(record) {
  const season = String(record.season || 0).padStart(2, '0');
  const episodes = (record.episodes || []).map(number => `E${String(number).padStart(2, '0')}`).join(', ');
  return `S${season} ${episodes}`
}

function resourceRecordIdentity(record) {
  const name = record.media_title || '中文名待识别';
  const episode = record.media_type === 'movie' ? '电影' : channelRecordEpisodes(record);
  return [name, episode, record.tmdb_id ? `TMDB ${record.tmdb_id}` : '身份待确认'].join(' · ')
}

function channelRecordSpecs(record) {
  const specs = record.specs || {};
  return channelDedupAttributes
    .map(({ value, title }) => {
      const item = specs[value];
      if (item === null || item === undefined || item === '') return ''
      if (value === 'size') return `${title} ${formatBytes(item)}`
      if (value === 'fps') return `${title} ${item} FPS`
      return `${title} ${item}`
    })
    .filter(Boolean)
    .join(' · ')
}

// 删除当前没有活跃种子的任务。
async function confirmDeleteTask() {
  saving.value = true;
  try {
    unwrapResponse(await props.api.delete(`${pluginBase.value}/tasks/${selectedTaskId.value}`));
    deleteDialog.value = false;
    selectedTaskId.value = '';
    await loadStatus({ preserveSelection: false });
    notify('刷流任务已删除');
  } catch (err) {
    notify(err?.message || '删除刷流任务失败', 'error');
  } finally {
    saving.value = false;
  }
}

// 清空当前任务的统计、诊断与种子记录。
async function confirmClearTask() {
  saving.value = true;
  try {
    taskDetail.value = unwrapResponse(
      await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/clear`, {}),
    );
    clearDialog.value = false;
    await loadStatus();
    notify('任务数据已清除');
  } catch (err) {
    notify(err?.message || '清除任务数据失败', 'error');
  } finally {
    saving.value = false;
  }
}

// 切换活跃或已删除种子分页视图。
async function changeTorrentState(value) {
  torrentState.value = value;
  torrentPage.value = 1;
  await loadTaskDetail();
}

// 加载指定页的种子记录。
async function changeTorrentPage(value) {
  torrentPage.value = value;
  await loadTaskDetail();
}

// 根据配置生成当前种子的下一项处理条件摘要。
function torrentPolicy(item) {
  if (item.deleted) return '已删除'
  if (item.hit_and_run && taskConfig.value.hr_seed_time) return `H&R ${taskConfig.value.hr_seed_time} 小时`
  if (taskConfig.value.seed_time) return `${taskConfig.value.seed_time} 小时后检查`
  if (taskConfig.value.seed_ratio) return `分享率 ${taskConfig.value.seed_ratio}`
  return taskConfig.value.proxy_delete ? '动态删种托管' : '等待删除条件'
}

// 返回种子当前下载或做种状态文本。
function torrentStateText(item) {
  const progress = torrentProgress(item);
  if (item.deleted) return '已删除'
  return progress >= 100 ? '做种' : `下载 ${progress}%`
}

// 将站点分享率状态格式化为当前值、无限或暂无数据。
function formatSiteRatio(siteRatio) {
  if (!siteRatio?.available) return '-'
  if (siteRatio.unlimited) return '∞'
  return Number(siteRatio.current || 0).toFixed(2)
}

watch(
  () => props.initialTab,
  value => {
    if (value) activeTab.value = value;
  },
);

onMounted(async () => {
  await loadStatus({ preserveSelection: false });
  refreshTimer = window.setInterval(() => {
    if (!editorOpen.value && !settingsDialog.value && document.visibilityState === 'visible') loadStatus();
    if (channelDedupDialog.value && document.visibilityState === 'visible') refreshApprovalRecords();
  }, 30000);
});

onUnmounted(() => {
  clearTimeout(resourceSearchTimer);
  resourceGeneration += 1;
  if (refreshTimer) window.clearInterval(refreshTimer);
});

__expose({ loadStatus, refreshAll, loading, saving });

return (_ctx, _cache) => {
  const _component_VIcon = _resolveComponent("VIcon");
  const _component_VChip = _resolveComponent("VChip");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VTooltip = _resolveComponent("VTooltip");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VSkeletonLoader = _resolveComponent("VSkeletonLoader");
  const _component_VListItem = _resolveComponent("VListItem");
  const _component_VSelect = _resolveComponent("VSelect");
  const _component_VSheet = _resolveComponent("VSheet");
  const _component_VAvatar = _resolveComponent("VAvatar");
  const _component_VTab = _resolveComponent("VTab");
  const _component_VTabs = _resolveComponent("VTabs");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");
  const _component_VBtnToggle = _resolveComponent("VBtnToggle");
  const _component_VDataTable = _resolveComponent("VDataTable");
  const _component_VPagination = _resolveComponent("VPagination");
  const _component_VWindowItem = _resolveComponent("VWindowItem");
  const _component_VWindow = _resolveComponent("VWindow");
  const _component_VCardTitle = _resolveComponent("VCardTitle");
  const _component_VSwitch = _resolveComponent("VSwitch");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VCheckbox = _resolveComponent("VCheckbox");
  const _component_VTextarea = _resolveComponent("VTextarea");
  const _component_VCardText = _resolveComponent("VCardText");
  const _component_VCard = _resolveComponent("VCard");
  const _component_VDialog = _resolveComponent("VDialog");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VProgressCircular = _resolveComponent("VProgressCircular");
  const _component_VList = _resolveComponent("VList");
  const _component_VCardActions = _resolveComponent("VCardActions");

  return (_openBlock(), _createElementBlock("div", {
    class: _normalizeClass(["resourceflow-page", { 'resourceflow-page--compact': __props.compact }])
  }, [
    _createElementVNode("header", _hoisted_1, [
      _createElementVNode("div", _hoisted_2, [
        _createVNode(_component_VIcon, {
          icon: "mdi-sync",
          color: "primary",
          size: "28"
        }),
        _cache[52] || (_cache[52] = _createElementVNode("div", null, [
          _createElementVNode("h1", null, "资源刷流"),
          _createElementVNode("p", null, "多站点任务独立调度与托管")
        ], -1))
      ]),
      _createElementVNode("div", _hoisted_3, [
        (status.value.summary.task_count)
          ? (_openBlock(), _createBlock(_component_VChip, {
              key: 0,
              size: "small",
              variant: "tonal"
            }, {
              default: _withCtx(() => [
                _createTextVNode(_toDisplayString(status.value.summary.running_count ?? status.value.summary.enabled_count ?? 0) + " / " + _toDisplayString(status.value.summary.task_count) + " 运行 ", 1)
              ]),
              _: 1
            }))
          : _createCommentVNode("", true),
        _createVNode(_component_VTooltip, { text: "共享下载审批" }, {
          activator: _withCtx(({ props: tooltipProps }) => [
            _createVNode(_component_VBtn, _mergeProps(tooltipProps, {
              icon: "mdi-send",
              variant: "text",
              "aria-label": "共享下载审批",
              onClick: openChannelDedup
            }), null, 16)
          ]),
          _: 1
        }),
        _createVNode(_component_VTooltip, { text: "全局设置" }, {
          activator: _withCtx(({ props: tooltipProps }) => [
            _createVNode(_component_VBtn, _mergeProps(tooltipProps, {
              icon: "mdi-tune-variant",
              variant: "text",
              "aria-label": "全局设置",
              onClick: openSettings
            }), null, 16)
          ]),
          _: 1
        }),
        (__props.showClose)
          ? (_openBlock(), _createBlock(_component_VBtn, {
              key: 1,
              icon: "mdi-close",
              variant: "text",
              "aria-label": "关闭",
              onClick: _cache[0] || (_cache[0] = $event => (emit('close')))
            }))
          : _createCommentVNode("", true)
      ])
    ]),
    (error.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 0,
          type: "error",
          variant: "tonal",
          closable: "",
          "onClick:close": _cache[1] || (_cache[1] = $event => (error.value = ''))
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(error.value), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (statusLoaded.value && !status.value.enabled)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 1,
          type: "warning",
          variant: "tonal"
        }, {
          default: _withCtx(() => [...(_cache[53] || (_cache[53] = [
            _createTextVNode(" 插件当前未启用，任务配置与历史仍可查看，启用后才会注册刷新和检查服务。 ", -1)
          ]))]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (loading.value && !tasks.value.length)
      ? (_openBlock(), _createElementBlock("div", _hoisted_4, [
          _createVNode(_component_VSkeletonLoader, { type: "list-item-three-line, list-item-three-line, article" })
        ]))
      : (!tasks.value.length)
        ? (_openBlock(), _createElementBlock("div", _hoisted_5, [
            _createVNode(_component_VIcon, {
              icon: "mdi-sync-off",
              size: "52",
              color: "medium-emphasis"
            }),
            _cache[55] || (_cache[55] = _createElementVNode("div", { class: "text-h6" }, "还没有刷流任务", -1)),
            _cache[56] || (_cache[56] = _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "创建任务后可为每个站点分别设置刷新、筛选和删种规则", -1)),
            _createVNode(_component_VBtn, {
              color: "primary",
              variant: "flat",
              "prepend-icon": "mdi-plus",
              onClick: openCreateTask
            }, {
              default: _withCtx(() => [...(_cache[54] || (_cache[54] = [
                _createTextVNode("创建第一个任务", -1)
              ]))]),
              _: 1
            })
          ]))
        : (_openBlock(), _createElementBlock(_Fragment, { key: 4 }, [
            _createVNode(_component_VSelect, {
              class: "resourceflow-mobile-select",
              "model-value": selectedTaskId.value,
              items: tasks.value,
              "item-title": "name",
              "item-value": "id",
              label: "当前任务",
              "hide-details": "",
              "onUpdate:modelValue": selectTask
            }, {
              item: _withCtx(({ props: itemProps, item }) => [
                _createVNode(_component_VListItem, _mergeProps(itemProps, {
                  subtitle: item.raw.site_name
                }), {
                  prepend: _withCtx(() => [
                    _createVNode(_component_VIcon, {
                      icon: _unref(taskStateMeta)(item.raw.state).icon,
                      color: _unref(taskStateMeta)(item.raw.state).color
                    }, null, 8, ["icon", "color"])
                  ]),
                  _: 2
                }, 1040, ["subtitle"])
              ]),
              _: 1
            }, 8, ["model-value", "items"]),
            _createElementVNode("div", _hoisted_6, [
              _createVNode(_component_VSheet, {
                tag: "aside",
                class: "resourceflow-task-rail app-surface-static"
              }, {
                default: _withCtx(() => [
                  _createElementVNode("div", _hoisted_7, [
                    _cache[57] || (_cache[57] = _createElementVNode("span", { class: "text-subtitle-2" }, "刷流任务", -1)),
                    _createVNode(_component_VChip, {
                      size: "x-small",
                      variant: "tonal"
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(tasks.value.length), 1)
                      ]),
                      _: 1
                    })
                  ]),
                  _createElementVNode("div", _hoisted_8, [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(tasks.value, (task) => {
                      return (_openBlock(), _createElementBlock("button", {
                        key: task.id,
                        type: "button",
                        class: _normalizeClass(["resourceflow-task-item", { 'resourceflow-task-item--selected': task.id === selectedTaskId.value }]),
                        "aria-pressed": task.id === selectedTaskId.value,
                        onClick: $event => (selectTask(task.id))
                      }, [
                        _createElementVNode("span", _hoisted_10, [
                          _createElementVNode("strong", null, _toDisplayString(task.name), 1),
                          _createElementVNode("span", {
                            class: _normalizeClass(["resourceflow-status-dot", `resourceflow-status-dot--${_unref(taskStateMeta)(task.state).color}`])
                          }, null, 2)
                        ]),
                        _createElementVNode("span", null, _toDisplayString(task.spec_dedup_enabled ? '下载审批 · ' : '') + _toDisplayString(task.site_name) + " · " + _toDisplayString(task.downloader), 1),
                        _createElementVNode("span", _hoisted_11, [
                          _createElementVNode("span", null, _toDisplayString(task.statistic.active || 0) + " 个种子", 1),
                          _createElementVNode("span", null, _toDisplayString(task.next_run_at ? _unref(formatDateTime)(task.next_run_at) : '暂无计划'), 1)
                        ])
                      ], 10, _hoisted_9))
                    }), 128))
                  ]),
                  _createVNode(_component_VBtn, {
                    class: "resourceflow-create-task",
                    block: "",
                    variant: "tonal",
                    "prepend-icon": "mdi-plus",
                    onClick: openCreateTask
                  }, {
                    default: _withCtx(() => [...(_cache[58] || (_cache[58] = [
                      _createTextVNode(" 新建任务 ", -1)
                    ]))]),
                    _: 1
                  })
                ]),
                _: 1
              }),
              (selectedTask.value)
                ? (_openBlock(), _createElementBlock("main", _hoisted_12, [
                    _createElementVNode("section", _hoisted_13, [
                      _createElementVNode("div", _hoisted_14, [
                        _createVNode(_component_VAvatar, {
                          color: "primary",
                          variant: "tonal",
                          rounded: "",
                          size: "42"
                        }, {
                          default: _withCtx(() => [
                            _createVNode(_component_VIcon, { icon: "mdi-web" })
                          ]),
                          _: 1
                        }),
                        _createElementVNode("div", null, [
                          _createElementVNode("div", _hoisted_15, [
                            _createElementVNode("h2", null, _toDisplayString(selectedTask.value.name), 1),
                            (selectedTask.value.spec_dedup_enabled)
                              ? (_openBlock(), _createBlock(_component_VChip, {
                                  key: 0,
                                  size: "small",
                                  variant: "outlined"
                                }, {
                                  default: _withCtx(() => [...(_cache[59] || (_cache[59] = [
                                    _createTextVNode("下载审批", -1)
                                  ]))]),
                                  _: 1
                                }))
                              : _createCommentVNode("", true),
                            _createVNode(_component_VChip, {
                              color: selectedState.value.color,
                              size: "small",
                              variant: "tonal",
                              "prepend-icon": selectedState.value.icon
                            }, {
                              default: _withCtx(() => [
                                _createTextVNode(_toDisplayString(selectedState.value.text), 1)
                              ]),
                              _: 1
                            }, 8, ["color", "prepend-icon"])
                          ]),
                          _createElementVNode("p", null, _toDisplayString(selectedTask.value.site_name) + " · 最近 " + _toDisplayString(selectedTask.value.last_run ? _unref(formatDateTime)(selectedTask.value.last_run.started_at) : '尚未运行') + " · 下次 " + _toDisplayString(selectedTask.value.next_run_at ? _unref(formatDateTime)(selectedTask.value.next_run_at) : '暂无计划'), 1)
                        ])
                      ]),
                      _createElementVNode("div", _hoisted_16, [
                        _createVNode(_component_VTooltip, { text: "立即刷新" }, {
                          activator: _withCtx(({ props: tipProps }) => [
                            _createVNode(_component_VBtn, _mergeProps(tipProps, {
                              icon: "mdi-sync",
                              variant: "text",
                              loading: selectedTask.value.operation === 'brush',
                              onClick: _cache[2] || (_cache[2] = $event => (runOperation('brush')))
                            }), null, 16, ["loading"])
                          ]),
                          _: 1
                        }),
                        _createVNode(_component_VTooltip, { text: "检查种子" }, {
                          activator: _withCtx(({ props: tipProps }) => [
                            _createVNode(_component_VBtn, _mergeProps(tipProps, {
                              icon: "mdi-progress-check",
                              variant: "text",
                              loading: selectedTask.value.operation === 'check',
                              onClick: _cache[3] || (_cache[3] = $event => (runOperation('check')))
                            }), null, 16, ["loading"])
                          ]),
                          _: 1
                        }),
                        _createVNode(_component_VTooltip, {
                          text: selectedTask.value.enabled ? '暂停任务' : '启用任务'
                        }, {
                          activator: _withCtx(({ props: tipProps }) => [
                            _createVNode(_component_VBtn, _mergeProps(tipProps, {
                              icon: selectedTask.value.enabled ? 'mdi-pause' : 'mdi-play',
                              variant: "text",
                              onClick: toggleSelectedTask
                            }), null, 16, ["icon"])
                          ]),
                          _: 1
                        }, 8, ["text"]),
                        _createVNode(_component_VTooltip, { text: "编辑任务" }, {
                          activator: _withCtx(({ props: tipProps }) => [
                            _createVNode(_component_VBtn, _mergeProps(tipProps, {
                              icon: "mdi-pencil-outline",
                              variant: "text",
                              onClick: openEditTask
                            }), null, 16)
                          ]),
                          _: 1
                        })
                      ])
                    ]),
                    _createVNode(_component_VTabs, {
                      modelValue: activeTab.value,
                      "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((activeTab).value = $event)),
                      color: "primary",
                      class: "resourceflow-tabs"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_VTab, { value: "overview" }, {
                          default: _withCtx(() => [...(_cache[60] || (_cache[60] = [
                            _createTextVNode("任务概览", -1)
                          ]))]),
                          _: 1
                        }),
                        _createVNode(_component_VTab, { value: "diagnostics" }, {
                          default: _withCtx(() => [...(_cache[61] || (_cache[61] = [
                            _createTextVNode("运行诊断", -1)
                          ]))]),
                          _: 1
                        }),
                        _createVNode(_component_VTab, { value: "config" }, {
                          default: _withCtx(() => [...(_cache[62] || (_cache[62] = [
                            _createTextVNode("任务配置", -1)
                          ]))]),
                          _: 1
                        })
                      ]),
                      _: 1
                    }, 8, ["modelValue"]),
                    _createVNode(_component_VDivider),
                    _createVNode(_component_VWindow, {
                      modelValue: activeTab.value,
                      "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((activeTab).value = $event)),
                      touch: false,
                      class: "resourceflow-window"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_VWindowItem, { value: "overview" }, {
                          default: _withCtx(() => [
                            _createElementVNode("div", _hoisted_17, [
                              _createVNode(_component_VSheet, { class: "resourceflow-stat app-surface-static" }, {
                                default: _withCtx(() => [
                                  _cache[63] || (_cache[63] = _createElementVNode("span", null, "活跃种子", -1)),
                                  _createElementVNode("strong", null, _toDisplayString(selectedTask.value.statistic.active || 0), 1),
                                  _createElementVNode("small", null, _toDisplayString(selectedTask.value.statistic.unarchived || 0) + " 个待归档", 1)
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, { class: "resourceflow-stat app-surface-static" }, {
                                default: _withCtx(() => [
                                  _cache[64] || (_cache[64] = _createElementVNode("span", null, "累计上传 / 下载", -1)),
                                  _createElementVNode("strong", null, _toDisplayString(_unref(formatBytes)(selectedTask.value.statistic.uploaded)) + " / " + _toDisplayString(_unref(formatBytes)(selectedTask.value.statistic.downloaded)), 1),
                                  _cache[65] || (_cache[65] = _createElementVNode("small", null, "当前任务累计", -1))
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, { class: "resourceflow-stat app-surface-static" }, {
                                default: _withCtx(() => [
                                  _cache[66] || (_cache[66] = _createElementVNode("span", null, "当前做种", -1)),
                                  _createElementVNode("strong", null, _toDisplayString(_unref(formatBytes)(selectedTask.value.seeding_size)), 1),
                                  _createElementVNode("small", null, _toDisplayString(taskConfig.value.disksize ? `上限 ${taskConfig.value.disksize} GB` : '未设置体积上限'), 1),
                                  (taskConfig.value.disksize)
                                    ? (_openBlock(), _createBlock(_component_VProgressLinear, {
                                        key: 0,
                                        "model-value": seedingPercent.value,
                                        height: "4",
                                        color: "primary"
                                      }, null, 8, ["model-value"]))
                                    : _createCommentVNode("", true)
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, { class: "resourceflow-stat app-surface-static" }, {
                                default: _withCtx(() => [
                                  _createElementVNode("span", null, _toDisplayString(taskConfig.value.site_ratio_control ? '站点分享率' : '最近刷新'), 1),
                                  _createElementVNode("strong", null, _toDisplayString(taskConfig.value.site_ratio_control ? formatSiteRatio(selectedTask.value.site_ratio) : latestBrushRun.value ? `${latestBrushRun.value.added_count || 0} / ${latestBrushRun.value.source_count || 0}` : '-'), 1),
                                  (taskConfig.value.site_ratio_control)
                                    ? (_openBlock(), _createElementBlock("small", _hoisted_18, _toDisplayString(selectedTask.value.site_ratio?.available ? `目标 ${Number(taskConfig.value.site_ratio_target || 0).toFixed(2)}` : '暂无站点统计数据'), 1))
                                    : (_openBlock(), _createElementBlock("small", _hoisted_19, "新增 / 候选"))
                                ]),
                                _: 1
                              })
                            ]),
                            _createElementVNode("div", _hoisted_20, [
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _createElementVNode("header", _hoisted_21, [
                                    _cache[67] || (_cache[67] = _createElementVNode("div", null, [
                                      _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "运行状态"),
                                      _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "当前任务调度与核心策略")
                                    ], -1)),
                                    _createVNode(_component_VChip, {
                                      color: selectedState.value.color,
                                      size: "small",
                                      variant: "tonal"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(selectedState.value.text), 1)
                                      ]),
                                      _: 1
                                    }, 8, ["color"])
                                  ]),
                                  _createElementVNode("dl", _hoisted_22, [
                                    _createElementVNode("div", null, [
                                      _cache[68] || (_cache[68] = _createElementVNode("dt", null, "刷新周期", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.cron || `每 ${selectedTask.value.brush_interval} 分钟`), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[69] || (_cache[69] = _createElementVNode("dt", null, "检查周期", -1)),
                                      _createElementVNode("dd", null, "每 " + _toDisplayString(selectedTask.value.check_interval) + " 分钟", 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[70] || (_cache[70] = _createElementVNode("dt", null, "开启时段", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.active_time_range || '全天'), 1)
                                    ]),
                                    (taskConfig.value.site_ratio_control)
                                      ? (_openBlock(), _createElementBlock("div", _hoisted_23, [
                                          _cache[71] || (_cache[71] = _createElementVNode("dt", null, "站点分享率", -1)),
                                          _createElementVNode("dd", null, _toDisplayString(formatSiteRatio(selectedTask.value.site_ratio)) + " / " + _toDisplayString(Number(taskConfig.value.site_ratio_target || 0).toFixed(2)), 1)
                                        ]))
                                      : _createCommentVNode("", true),
                                    _createElementVNode("div", null, [
                                      _cache[72] || (_cache[72] = _createElementVNode("dt", null, "选种来源", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.rss_support ? 'RSS' : '站点列表页'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[73] || (_cache[73] = _createElementVNode("dt", null, "促销要求", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.freeleech === '2xfree' ? '2X 免费' : taskConfig.value.freeleech === 'free' ? '免费' : '全部'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[74] || (_cache[74] = _createElementVNode("dt", null, "删种策略", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.proxy_delete ? `动态 ${taskConfig.value.delete_size_range || '-' } GB` : '满足任一条件'), 1)
                                    ])
                                  ])
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _createElementVNode("header", _hoisted_24, [
                                    _createElementVNode("div", null, [
                                      _cache[75] || (_cache[75] = _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "最近一次刷新", -1)),
                                      _createElementVNode("div", _hoisted_25, _toDisplayString(latestBrushRun.value ? `${_unref(formatDateTime)(latestBrushRun.value.started_at)} · ${_unref(formatDuration)(latestBrushRun.value.started_at, latestBrushRun.value.finished_at)}` : '暂无运行记录'), 1)
                                    ]),
                                    (latestBrushRun.value)
                                      ? (_openBlock(), _createBlock(_component_VChip, {
                                          key: 0,
                                          color: latestBrushRun.value.success === false ? 'error' : 'success',
                                          size: "small",
                                          variant: "tonal"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(latestBrushRun.value.success === false ? '失败' : '完成'), 1)
                                          ]),
                                          _: 1
                                        }, 8, ["color"]))
                                      : _createCommentVNode("", true)
                                  ]),
                                  _createElementVNode("div", _hoisted_26, [
                                    _createElementVNode("div", null, [
                                      _cache[76] || (_cache[76] = _createElementVNode("span", null, "站点候选", -1)),
                                      _createElementVNode("strong", null, _toDisplayString(latestBrushRun.value?.source_count || 0), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[77] || (_cache[77] = _createElementVNode("span", null, "规则过滤", -1)),
                                      _createElementVNode("strong", null, _toDisplayString(latestBrushRun.value?.filtered_count || 0), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[78] || (_cache[78] = _createElementVNode("span", null, "新增下载", -1)),
                                      _createElementVNode("strong", null, _toDisplayString(latestBrushRun.value?.added_count || 0), 1)
                                    ])
                                  ]),
                                  (latestBrushRun.value?.error)
                                    ? (_openBlock(), _createBlock(_component_VAlert, {
                                        key: 0,
                                        type: "error",
                                        variant: "tonal",
                                        density: "compact"
                                      }, {
                                        default: _withCtx(() => [
                                          _createTextVNode(_toDisplayString(latestBrushRun.value.error), 1)
                                        ]),
                                        _: 1
                                      }))
                                    : _createCommentVNode("", true),
                                  _createVNode(_component_VBtn, {
                                    variant: "text",
                                    color: "primary",
                                    "append-icon": "mdi-arrow-right",
                                    onClick: _cache[5] || (_cache[5] = $event => (activeTab.value = 'diagnostics'))
                                  }, {
                                    default: _withCtx(() => [...(_cache[79] || (_cache[79] = [
                                      _createTextVNode(" 查看运行诊断 ", -1)
                                    ]))]),
                                    _: 1
                                  })
                                ]),
                                _: 1
                              })
                            ]),
                            _createVNode(_component_VSheet, {
                              tag: "section",
                              class: "resourceflow-panel resourceflow-torrents app-surface-static"
                            }, {
                              default: _withCtx(() => [
                                _createElementVNode("header", _hoisted_27, [
                                  _createElementVNode("div", null, [
                                    _createElementVNode("div", _hoisted_28, [
                                      _cache[80] || (_cache[80] = _createElementVNode("span", { class: "text-subtitle-1 font-weight-medium" }, "托管种子", -1)),
                                      _createVNode(_component_VChip, {
                                        size: "x-small",
                                        variant: "tonal"
                                      }, {
                                        default: _withCtx(() => [
                                          _createTextVNode(_toDisplayString(torrentData.value.total), 1)
                                        ]),
                                        _: 1
                                      })
                                    ]),
                                    _cache[81] || (_cache[81] = _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "当前任务独立记录", -1))
                                  ]),
                                  _createVNode(_component_VBtnToggle, {
                                    "model-value": torrentState.value,
                                    mandatory: "",
                                    color: "primary",
                                    density: "compact",
                                    "onUpdate:modelValue": changeTorrentState
                                  }, {
                                    default: _withCtx(() => [
                                      _createVNode(_component_VBtn, { value: "active" }, {
                                        default: _withCtx(() => [...(_cache[82] || (_cache[82] = [
                                          _createTextVNode("活跃", -1)
                                        ]))]),
                                        _: 1
                                      }),
                                      _createVNode(_component_VBtn, { value: "deleted" }, {
                                        default: _withCtx(() => [...(_cache[83] || (_cache[83] = [
                                          _createTextVNode("已删除", -1)
                                        ]))]),
                                        _: 1
                                      }),
                                      _createVNode(_component_VBtn, { value: "all" }, {
                                        default: _withCtx(() => [...(_cache[84] || (_cache[84] = [
                                          _createTextVNode("全部", -1)
                                        ]))]),
                                        _: 1
                                      })
                                    ]),
                                    _: 1
                                  }, 8, ["model-value"])
                                ]),
                                _createVNode(_component_VDataTable, {
                                  class: "resourceflow-torrent-table",
                                  headers: torrentHeaders,
                                  items: torrentData.value.items,
                                  loading: taskLoading.value,
                                  "items-per-page": -1,
                                  "hide-default-footer": "",
                                  density: "comfortable"
                                }, {
                                  "item.title": _withCtx(({ item }) => [
                                    _createElementVNode("div", _hoisted_29, [
                                      _createElementVNode("strong", null, _toDisplayString(item.title || '未知种子'), 1),
                                      _createElementVNode("span", null, _toDisplayString(item.site_name) + " · " + _toDisplayString(_unref(formatDateTime)((item.time || 0) * 1000)), 1)
                                    ])
                                  ]),
                                  "item.status": _withCtx(({ item }) => [
                                    _createVNode(_component_VChip, {
                                      size: "small",
                                      color: item.deleted ? 'secondary' : _unref(torrentProgress)(item) >= 100 ? 'success' : 'info',
                                      variant: "tonal"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(torrentStateText(item)), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["color"])
                                  ]),
                                  "item.size": _withCtx(({ item }) => [
                                    _createTextVNode(_toDisplayString(_unref(formatBytes)(item.size)), 1)
                                  ]),
                                  "item.uploaded": _withCtx(({ item }) => [
                                    _createTextVNode(_toDisplayString(_unref(formatBytes)(item.uploaded)), 1)
                                  ]),
                                  "item.ratio": _withCtx(({ item }) => [
                                    _createTextVNode(_toDisplayString(Number(item.ratio || 0).toFixed(2)), 1)
                                  ]),
                                  "item.policy": _withCtx(({ item }) => [
                                    _createTextVNode(_toDisplayString(torrentPolicy(item)), 1)
                                  ]),
                                  "no-data": _withCtx(() => [...(_cache[85] || (_cache[85] = [
                                    _createElementVNode("div", { class: "resourceflow-table-empty" }, "当前筛选下没有种子记录", -1)
                                  ]))]),
                                  _: 1
                                }, 8, ["items", "loading"]),
                                _createElementVNode("div", _hoisted_30, [
                                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(torrentData.value.items, (item) => {
                                    return (_openBlock(), _createElementBlock("article", {
                                      key: `${item.task_id}-${item.title}-${item.time}`,
                                      class: "resourceflow-mobile-torrent"
                                    }, [
                                      _createElementVNode("div", _hoisted_31, [
                                        _createElementVNode("strong", null, _toDisplayString(item.title || '未知种子'), 1),
                                        _createVNode(_component_VChip, {
                                          size: "x-small",
                                          variant: "tonal"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(torrentStateText(item)), 1)
                                          ]),
                                          _: 2
                                        }, 1024)
                                      ]),
                                      _createElementVNode("div", _hoisted_32, [
                                        _createElementVNode("span", null, _toDisplayString(_unref(formatBytes)(item.size)), 1),
                                        _createElementVNode("span", null, "上传 " + _toDisplayString(_unref(formatBytes)(item.uploaded)), 1),
                                        _createElementVNode("span", null, "分享率 " + _toDisplayString(Number(item.ratio || 0).toFixed(2)), 1)
                                      ]),
                                      _createElementVNode("div", _hoisted_33, _toDisplayString(torrentPolicy(item)), 1)
                                    ]))
                                  }), 128)),
                                  (!torrentData.value.items.length)
                                    ? (_openBlock(), _createElementBlock("div", _hoisted_34, "当前筛选下没有种子记录"))
                                    : _createCommentVNode("", true)
                                ]),
                                (totalTorrentPages.value > 1)
                                  ? (_openBlock(), _createBlock(_component_VPagination, {
                                      key: 0,
                                      "model-value": torrentPage.value,
                                      length: totalTorrentPages.value,
                                      "total-visible": 5,
                                      density: "comfortable",
                                      "onUpdate:modelValue": changeTorrentPage
                                    }, null, 8, ["model-value", "length"]))
                                  : _createCommentVNode("", true)
                              ]),
                              _: 1
                            })
                          ]),
                          _: 1
                        }),
                        _createVNode(_component_VWindowItem, { value: "diagnostics" }, {
                          default: _withCtx(() => [
                            _createElementVNode("div", _hoisted_35, [
                              _createElementVNode("div", null, [
                                _createElementVNode("div", _hoisted_36, _toDisplayString(latestBrushRun.value ? `刷流刷新 #${latestBrushRun.value.id.slice(0, 8)}` : '暂无刷流刷新记录'), 1),
                                _createElementVNode("div", _hoisted_37, _toDisplayString(latestBrushRun.value ? `${_unref(formatDateTime)(latestBrushRun.value.started_at)} · ${_unref(formatDuration)(latestBrushRun.value.started_at, latestBrushRun.value.finished_at)}` : '执行一次任务后将显示筛选流水线和过滤原因'), 1)
                              ]),
                              (latestBrushRun.value)
                                ? (_openBlock(), _createBlock(_component_VChip, {
                                    key: 0,
                                    color: latestBrushRun.value.success === false ? 'error' : 'success',
                                    variant: "tonal"
                                  }, {
                                    default: _withCtx(() => [
                                      _createTextVNode(_toDisplayString(latestBrushRun.value.success === false ? '失败' : '完成'), 1)
                                    ]),
                                    _: 1
                                  }, 8, ["color"]))
                                : _createCommentVNode("", true)
                            ]),
                            _createElementVNode("div", _hoisted_38, [
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _cache[86] || (_cache[86] = _createElementVNode("header", { class: "resourceflow-panel__head" }, [
                                    _createElementVNode("div", null, [
                                      _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "选种流水线"),
                                      _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "本轮候选在各阶段的剩余数量")
                                    ])
                                  ], -1)),
                                  _createElementVNode("ol", _hoisted_39, [
                                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(pipelineStages.value, (stage, index) => {
                                      return (_openBlock(), _createElementBlock("li", {
                                        key: stage.title
                                      }, [
                                        _createElementVNode("span", _hoisted_40, _toDisplayString(index + 1), 1),
                                        _createElementVNode("div", null, [
                                          _createElementVNode("strong", null, _toDisplayString(stage.title), 1),
                                          _createElementVNode("span", null, _toDisplayString(stage.detail), 1)
                                        ]),
                                        _createVNode(_component_VChip, {
                                          size: "small",
                                          variant: "tonal"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(stage.count), 1)
                                          ]),
                                          _: 2
                                        }, 1024)
                                      ]))
                                    }), 128))
                                  ])
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _cache[87] || (_cache[87] = _createElementVNode("header", { class: "resourceflow-panel__head" }, [
                                    _createElementVNode("div", null, [
                                      _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "过滤原因"),
                                      _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "本轮未进入下载器的候选分布")
                                    ])
                                  ], -1)),
                                  (reasonEntries.value.length)
                                    ? (_openBlock(), _createElementBlock("div", _hoisted_41, [
                                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(reasonEntries.value, (item) => {
                                          return (_openBlock(), _createElementBlock("div", {
                                            key: item.label,
                                            class: "resourceflow-reason"
                                          }, [
                                            _createElementVNode("div", null, [
                                              _createElementVNode("span", null, _toDisplayString(item.label), 1),
                                              _createElementVNode("strong", null, _toDisplayString(item.count), 1)
                                            ]),
                                            _createElementVNode("span", _hoisted_42, [
                                              _createElementVNode("i", {
                                                style: _normalizeStyle({ width: `${(item.count / maxReasonCount.value) * 100}%` })
                                              }, null, 4)
                                            ])
                                          ]))
                                        }), 128))
                                      ]))
                                    : (_openBlock(), _createElementBlock("div", _hoisted_43, "本轮没有记录过滤原因"))
                                ]),
                                _: 1
                              })
                            ]),
                            _createVNode(_component_VSheet, {
                              tag: "section",
                              class: "resourceflow-panel app-surface-static"
                            }, {
                              default: _withCtx(() => [
                                _cache[88] || (_cache[88] = _createElementVNode("header", { class: "resourceflow-panel__head" }, [
                                  _createElementVNode("div", null, [
                                    _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "最近事件"),
                                    _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "刷流刷新与种子检查独立记录")
                                  ])
                                ], -1)),
                                _createElementVNode("div", _hoisted_44, [
                                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(taskRuns.value, (run) => {
                                    return (_openBlock(), _createElementBlock("article", {
                                      key: run.id
                                    }, [
                                      _createVNode(_component_VIcon, {
                                        icon: run.success === false ? 'mdi-alert-circle-outline' : run.kind === 'brush' ? 'mdi-sync' : 'mdi-progress-check',
                                        color: run.success === false ? 'error' : run.kind === 'brush' ? 'primary' : 'info'
                                      }, null, 8, ["icon", "color"]),
                                      _createElementVNode("div", null, [
                                        _createElementVNode("strong", null, _toDisplayString(run.kind === 'brush' ? '刷流刷新' : '种子检查'), 1),
                                        _createElementVNode("span", null, _toDisplayString(_unref(formatDateTime)(run.started_at)) + " · " + _toDisplayString(_unref(formatDuration)(run.started_at, run.finished_at)) + " · " + _toDisplayString(run.kind === 'brush' ? `新增 ${run.added_count || 0}，过滤 ${run.filtered_count || 0}` : `活跃 ${run.active_count || 0}，删除 ${run.deleted_count || 0}`), 1),
                                        (run.error)
                                          ? (_openBlock(), _createElementBlock("span", _hoisted_45, _toDisplayString(run.error), 1))
                                          : _createCommentVNode("", true)
                                      ])
                                    ]))
                                  }), 128)),
                                  (!taskRuns.value.length)
                                    ? (_openBlock(), _createElementBlock("div", _hoisted_46, "暂无运行事件"))
                                    : _createCommentVNode("", true)
                                ])
                              ]),
                              _: 1
                            })
                          ]),
                          _: 1
                        }),
                        _createVNode(_component_VWindowItem, { value: "config" }, {
                          default: _withCtx(() => [
                            _createElementVNode("div", _hoisted_47, [
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _cache[102] || (_cache[102] = _createElementVNode("header", { class: "resourceflow-panel__head" }, [
                                    _createElementVNode("div", null, [
                                      _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "任务规则"),
                                      _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "当前服务端生效配置")
                                    ])
                                  ], -1)),
                                  _createElementVNode("dl", _hoisted_48, [
                                    _createElementVNode("div", null, [
                                      _cache[89] || (_cache[89] = _createElementVNode("dt", null, "任务状态", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(selectedTask.value.enabled ? '启用' : '暂停'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[90] || (_cache[90] = _createElementVNode("dt", null, "通知", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.notify ? '发送' : '关闭'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[91] || (_cache[91] = _createElementVNode("dt", null, "站点", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(selectedTask.value.site_name), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[92] || (_cache[92] = _createElementVNode("dt", null, "下载器", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(selectedTask.value.downloader), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[93] || (_cache[93] = _createElementVNode("dt", null, "种子大小", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.size || '不限'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[94] || (_cache[94] = _createElementVNode("dt", null, "做种人数", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.seeder || '不限'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[95] || (_cache[95] = _createElementVNode("dt", null, "发布时间", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.pubtime ? `${taskConfig.value.pubtime} 分钟` : '不限'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[96] || (_cache[96] = _createElementVNode("dt", null, "排除 H&R", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.hr === 'yes' ? '是' : '否'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[97] || (_cache[97] = _createElementVNode("dt", null, "包含规则", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.include || '无'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[98] || (_cache[98] = _createElementVNode("dt", null, "排除规则", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.exclude || '无'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[99] || (_cache[99] = _createElementVNode("dt", null, "保种上限", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.disksize ? `${taskConfig.value.disksize} GB` : '不限'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[100] || (_cache[100] = _createElementVNode("dt", null, "目标分享率", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.site_ratio_control ? Number(taskConfig.value.site_ratio_target || 0).toFixed(2) : '关闭'), 1)
                                    ]),
                                    _createElementVNode("div", null, [
                                      _cache[101] || (_cache[101] = _createElementVNode("dt", null, "归档天数", -1)),
                                      _createElementVNode("dd", null, _toDisplayString(taskConfig.value.auto_archive_days || '不自动归档'), 1)
                                    ])
                                  ])
                                ]),
                                _: 1
                              }),
                              _createVNode(_component_VSheet, {
                                tag: "section",
                                class: "resourceflow-panel app-surface-static"
                              }, {
                                default: _withCtx(() => [
                                  _cache[109] || (_cache[109] = _createElementVNode("header", { class: "resourceflow-panel__head" }, [
                                    _createElementVNode("div", null, [
                                      _createElementVNode("div", { class: "text-subtitle-1 font-weight-medium" }, "任务数据"),
                                      _createElementVNode("div", { class: "text-body-2 text-medium-emphasis" }, "以下操作只影响当前任务")
                                    ])
                                  ], -1)),
                                  _createElementVNode("div", _hoisted_49, [
                                    _createElementVNode("div", null, [
                                      _cache[104] || (_cache[104] = _createElementVNode("strong", null, "清除统计与记录", -1)),
                                      _cache[105] || (_cache[105] = _createElementVNode("span", null, "下载器中的任务标签种子会在下次检查时重新纳入", -1)),
                                      _createVNode(_component_VBtn, {
                                        color: "warning",
                                        variant: "tonal",
                                        "prepend-icon": "mdi-eraser",
                                        onClick: _cache[6] || (_cache[6] = $event => (clearDialog.value = true))
                                      }, {
                                        default: _withCtx(() => [...(_cache[103] || (_cache[103] = [
                                          _createTextVNode("清除数据", -1)
                                        ]))]),
                                        _: 1
                                      })
                                    ]),
                                    _createVNode(_component_VDivider),
                                    _createElementVNode("div", null, [
                                      _cache[107] || (_cache[107] = _createElementVNode("strong", null, "删除任务", -1)),
                                      _cache[108] || (_cache[108] = _createElementVNode("span", null, "存在活跃种子时后端会拒绝删除，避免留下失管任务", -1)),
                                      _createVNode(_component_VBtn, {
                                        color: "error",
                                        variant: "tonal",
                                        "prepend-icon": "mdi-delete-outline",
                                        onClick: _cache[7] || (_cache[7] = $event => (deleteDialog.value = true))
                                      }, {
                                        default: _withCtx(() => [...(_cache[106] || (_cache[106] = [
                                          _createTextVNode("删除任务", -1)
                                        ]))]),
                                        _: 1
                                      })
                                    ])
                                  ])
                                ]),
                                _: 1
                              })
                            ])
                          ]),
                          _: 1
                        })
                      ]),
                      _: 1
                    }, 8, ["modelValue"])
                  ]))
                : _createCommentVNode("", true)
            ])
          ], 64)),
    _createVNode(TaskEditorDialog, {
      modelValue: editorOpen.value,
      "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((editorOpen).value = $event)),
      task: editorTask.value,
      sites: status.value.options.sites,
      downloaders: status.value.options.downloaders,
      "global-dynamic-delete": Boolean(status.value.global_proxy_delete),
      saving: saving.value,
      onSave: saveTask,
      onDedupSettings: openSettings
    }, null, 8, ["modelValue", "task", "sites", "downloaders", "global-dynamic-delete", "saving"]),
    _createVNode(_component_VDialog, {
      modelValue: settingsDialog.value,
      "onUpdate:modelValue": _cache[30] || (_cache[30] = $event => ((settingsDialog).value = $event)),
      width: _ctx.$vuetify.display.xs ? '100vw' : '94vw',
      "max-width": _ctx.$vuetify.display.xs ? '100vw' : '80rem',
      height: _ctx.$vuetify.display.xs ? '100dvh' : '92dvh',
      fullscreen: _ctx.$vuetify.display.xs,
      persistent: saving.value,
      scrollable: ""
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { class: "channel-dedup__card" }, {
          default: _withCtx(() => [
            _createVNode(_component_VCardTitle, { class: "channel-dedup__title" }, {
              default: _withCtx(() => [
                _createVNode(_component_VIcon, {
                  icon: "mdi-tune-variant",
                  color: "primary"
                }),
                _cache[110] || (_cache[110] = _createElementVNode("span", null, "全局设置", -1))
              ]),
              _: 1
            }),
            _createVNode(_component_VCardText, { class: "channel-dedup__body global-settings__body" }, {
              default: _withCtx(() => [
                (settingsError.value || channelDedupError.value)
                  ? (_openBlock(), _createBlock(_component_VAlert, {
                      key: 0,
                      type: "error",
                      variant: "tonal",
                      density: "compact"
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(settingsError.value || channelDedupError.value), 1)
                      ]),
                      _: 1
                    }))
                  : _createCommentVNode("", true),
                _createElementVNode("div", _hoisted_50, [
                  _createVNode(_component_VProgressLinear, {
                    class: _normalizeClass({ 'global-settings__progress--idle': !loading.value && !channelDedupLoading.value }),
                    indeterminate: ""
                  }, null, 8, ["class"])
                ]),
                _createElementVNode("div", _hoisted_51, [
                  _createElementVNode("div", _hoisted_52, [
                    _createVNode(_component_VSwitch, {
                      modelValue: settingsDraft.value.enabled,
                      "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => ((settingsDraft.value.enabled) = $event)),
                      label: "启用插件",
                      color: "primary",
                      density: "compact",
                      "hide-details": "",
                      inset: ""
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VSwitch, {
                      modelValue: settingsDraft.value.show_sidebar_nav,
                      "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((settingsDraft.value.show_sidebar_nav) = $event)),
                      label: "显示侧栏入口",
                      color: "primary",
                      density: "compact",
                      "hide-details": "",
                      inset: ""
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VSwitch, {
                      modelValue: settingsDraft.value.global_proxy_delete,
                      "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((settingsDraft.value.global_proxy_delete) = $event)),
                      label: "全局动态删种",
                      color: "primary",
                      density: "compact",
                      "hide-details": "",
                      inset: ""
                    }, null, 8, ["modelValue"])
                  ]),
                  (settingsDraft.value.global_proxy_delete)
                    ? (_openBlock(), _createElementBlock("div", _hoisted_53, [
                        _createVNode(_component_VTextField, {
                          modelValue: settingsDraft.value.global_delete_size_range,
                          "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((settingsDraft.value.global_delete_size_range) = $event)),
                          label: "全局动态删种阈值（GB）",
                          placeholder: "50-100",
                          density: "comfortable",
                          clearable: "",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]))
                    : _createCommentVNode("", true),
                  _createVNode(_component_VDivider),
                  _createElementVNode("div", _hoisted_54, [
                    _createVNode(_component_VTextField, {
                      modelValue: settingsDraft.value.global_disksize,
                      "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((settingsDraft.value.global_disksize) = $event)),
                      modelModifiers: { number: true },
                      type: "number",
                      min: "0",
                      label: "全局保种体积（GB）",
                      density: "comfortable",
                      clearable: "",
                      "hide-details": ""
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VTextField, {
                      modelValue: settingsDraft.value.global_maxdlcount,
                      "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((settingsDraft.value.global_maxdlcount) = $event)),
                      modelModifiers: { number: true },
                      type: "number",
                      min: "0",
                      label: "全局同时下载任务数",
                      density: "comfortable",
                      clearable: "",
                      "hide-details": ""
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VTextField, {
                      modelValue: settingsDraft.value.global_maxupspeed,
                      "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((settingsDraft.value.global_maxupspeed) = $event)),
                      modelModifiers: { number: true },
                      type: "number",
                      min: "0",
                      label: "全局总上传带宽（KB/s）",
                      density: "comfortable",
                      clearable: "",
                      "hide-details": ""
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VTextField, {
                      modelValue: settingsDraft.value.global_maxdlspeed,
                      "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((settingsDraft.value.global_maxdlspeed) = $event)),
                      modelModifiers: { number: true },
                      type: "number",
                      min: "0",
                      label: "全局总下载带宽（KB/s）",
                      density: "comfortable",
                      clearable: "",
                      "hide-details": ""
                    }, null, 8, ["modelValue"])
                  ]),
                  _createVNode(_component_VDivider),
                  _cache[111] || (_cache[111] = _createElementVNode("div", { class: "text-subtitle-2" }, "缺失播出日期兜底", -1)),
                  _createElementVNode("div", _hoisted_55, [
                    _createVNode(_component_VTextField, {
                      modelValue: settingsDraft.value.recent_missing_date_tolerance_days,
                      "onUpdate:modelValue": _cache[18] || (_cache[18] = $event => ((settingsDraft.value.recent_missing_date_tolerance_days) = $event)),
                      modelModifiers: { number: true },
                      type: "number",
                      min: "1",
                      max: "30",
                      step: "1",
                      label: "缺失日期容差天数",
                      hint: "仅在 TMDB、豆瓣都没有逐集日期时使用",
                      "persistent-hint": "",
                      density: "comfortable",
                      "hide-details": "auto"
                    }, null, 8, ["modelValue"]),
                    _createVNode(_component_VSwitch, {
                      modelValue: settingsDraft.value.recent_missing_date_ai_enabled,
                      "onUpdate:modelValue": _cache[19] || (_cache[19] = $event => ((settingsDraft.value.recent_missing_date_ai_enabled) = $event)),
                      label: "启用 AI 官方排播核验",
                      color: "primary",
                      density: "compact",
                      "hide-details": "",
                      inset: ""
                    }, null, 8, ["modelValue"])
                  ])
                ]),
                _createVNode(_component_VDivider),
                (settingsDialog.value)
                  ? (_openBlock(), _createBlock(SharedSettingsPanel, {
                      key: 1,
                      ref_key: "sharedSettingsPanel",
                      ref: sharedSettingsPanel,
                      mode: "settings",
                      api: __props.api,
                      "plugin-base": pluginBase.value,
                      onHostChange: _cache[20] || (_cache[20] = $event => (sharedHost.value = $event)),
                      onPolicySynced: _cache[21] || (_cache[21] = $event => (loadChannelDedup()))
                    }, null, 8, ["api", "plugin-base"]))
                  : _createCommentVNode("", true),
                (sharedHost.value)
                  ? (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [
                      _createVNode(_component_VDivider),
                      _createElementVNode("div", _hoisted_56, [
                        _cache[112] || (_cache[112] = _createElementVNode("div", { class: "text-subtitle-2" }, "全局去重字段", -1)),
                        _createElementVNode("div", _hoisted_57, [
                          _createElementVNode("div", _hoisted_58, [
                            (_openBlock(), _createElementBlock(_Fragment, null, _renderList(channelDedupAttributes, (attribute) => {
                              return _createVNode(_component_VCheckbox, {
                                key: attribute.value,
                                modelValue: channelDedupDraft.value.attributes,
                                "onUpdate:modelValue": _cache[22] || (_cache[22] = $event => ((channelDedupDraft.value.attributes) = $event)),
                                label: attribute.title,
                                value: attribute.value,
                                color: "primary",
                                density: "compact",
                                "hide-details": ""
                              }, null, 8, ["modelValue", "label", "value"])
                            }), 64))
                          ]),
                          _createVNode(_component_VSelect, {
                            modelValue: channelDedupDraft.value.unknown_policy,
                            "onUpdate:modelValue": _cache[23] || (_cache[23] = $event => ((channelDedupDraft.value.unknown_policy) = $event)),
                            items: channelUnknownPolicies,
                            class: "global-settings__unknown",
                            label: "候选缺少勾选规格时",
                            density: "comfortable",
                            disabled: !channelDedupDraft.value.attributes.length,
                            "hide-details": ""
                          }, null, 8, ["modelValue", "disabled"])
                        ])
                      ]),
                      (channelDedupDraft.value.attributes.includes('size'))
                        ? (_openBlock(), _createElementBlock("div", _hoisted_59, [
                            _createVNode(_component_VTextField, {
                              modelValue: channelDedupDraft.value.size_tolerance_pct,
                              "onUpdate:modelValue": _cache[24] || (_cache[24] = $event => ((channelDedupDraft.value.size_tolerance_pct) = $event)),
                              modelModifiers: { number: true },
                              type: "number",
                              min: "0",
                              max: "20",
                              step: "1",
                              suffix: "%",
                              label: "单集文件大小容差",
                              density: "comfortable",
                              hint: "按种子内单个视频文件大小比较，不使用整包总大小",
                              "persistent-hint": ""
                            }, null, 8, ["modelValue"])
                          ]))
                        : _createCommentVNode("", true),
                      _createVNode(_component_VDivider)
                    ], 64))
                  : _createCommentVNode("", true),
                _createVNode(_component_VDivider),
                _cache[113] || (_cache[113] = _createElementVNode("div", { class: "text-subtitle-2" }, "别名绑定", -1)),
                _createElementVNode("div", _hoisted_60, [
                  _createVNode(_component_VTextarea, {
                    modelValue: channelDedupDraft.value.identity_aliases,
                    "onUpdate:modelValue": _cache[25] || (_cache[25] = $event => ((channelDedupDraft.value.identity_aliases) = $event)),
                    label: "剧名别名绑定（可选）",
                    placeholder: "剧名别名 = TMDB剧集ID",
                    rows: "2",
                    density: "comfortable",
                    "auto-grow": "",
                    "hide-details": ""
                  }, null, 8, ["modelValue"]),
                  _createVNode(_component_VTextarea, {
                    modelValue: channelDedupDraft.value.movie_identity_aliases,
                    "onUpdate:modelValue": _cache[26] || (_cache[26] = $event => ((channelDedupDraft.value.movie_identity_aliases) = $event)),
                    label: "电影别名绑定（可选）",
                    placeholder: "电影别名 = TMDB电影ID",
                    rows: "2",
                    density: "comfortable",
                    "auto-grow": "",
                    "hide-details": ""
                  }, null, 8, ["modelValue"])
                ])
              ]),
              _: 1
            }),
            _createElementVNode("footer", _hoisted_61, [
              _createElementVNode("div", _hoisted_62, [
                _createVNode(_component_VTooltip, { text: "测试审批连接" }, {
                  activator: _withCtx(({ props: tip }) => [
                    _createVNode(_component_VBtn, _mergeProps(tip, {
                      "aria-label": "测试审批连接",
                      variant: "text",
                      icon: _ctx.$vuetify.display.xs ? 'mdi-connection' : undefined,
                      "prepend-icon": _ctx.$vuetify.display.xs ? undefined : 'mdi-connection',
                      disabled: saving.value || !sharedSettingsPanel.value?.ready || sharedSettingsPanel.value?.busy || !sharedSettingsPanel.value?.canTest,
                      onClick: _cache[27] || (_cache[27] = $event => (sharedSettingsPanel.value?.test()))
                    }), {
                      default: _withCtx(() => [
                        (_ctx.$vuetify.display.xs)
                          ? (_openBlock(), _createBlock(_component_VIcon, {
                              key: 0,
                              icon: "mdi-connection"
                            }))
                          : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                              _createTextVNode("测试审批连接")
                            ], 64))
                      ]),
                      _: 1
                    }, 16, ["icon", "prepend-icon", "disabled"])
                  ]),
                  _: 1
                }),
                _createVNode(_component_VTooltip, { text: "刷新共享状态" }, {
                  activator: _withCtx(({ props: tip }) => [
                    _createVNode(_component_VBtn, _mergeProps(tip, {
                      icon: "mdi-refresh",
                      "aria-label": "刷新共享状态",
                      variant: "text",
                      disabled: saving.value || sharedSettingsPanel.value?.busy,
                      onClick: _cache[28] || (_cache[28] = $event => (sharedSettingsPanel.value?.refresh()))
                    }), null, 16, ["disabled"])
                  ]),
                  _: 1
                })
              ]),
              _createElementVNode("div", _hoisted_63, [
                _createVNode(_component_VBtn, {
                  variant: "text",
                  disabled: saving.value || sharedSettingsPanel.value?.busy,
                  onClick: _cache[29] || (_cache[29] = $event => (settingsDialog.value = false))
                }, {
                  default: _withCtx(() => [...(_cache[114] || (_cache[114] = [
                    _createTextVNode("关闭", -1)
                  ]))]),
                  _: 1
                }, 8, ["disabled"]),
                _createVNode(_component_VBtn, {
                  color: "primary",
                  variant: "flat",
                  loading: saving.value,
                  disabled: !settingsReady.value || channelDedupLoading.value || sharedSettingsPanel.value?.busy,
                  onClick: saveSettings
                }, {
                  default: _withCtx(() => [...(_cache[115] || (_cache[115] = [
                    _createTextVNode("保存设置", -1)
                  ]))]),
                  _: 1
                }, 8, ["loading", "disabled"])
              ])
            ])
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue", "width", "max-width", "height", "fullscreen", "persistent"]),
    _createVNode(_component_VDialog, {
      modelValue: channelDedupDialog.value,
      "onUpdate:modelValue": _cache[38] || (_cache[38] = $event => ((channelDedupDialog).value = $event)),
      width: _ctx.$vuetify.display.xs ? '100vw' : '94vw',
      "max-width": _ctx.$vuetify.display.xs ? '100vw' : '80rem',
      height: _ctx.$vuetify.display.xs ? '100dvh' : '92dvh',
      fullscreen: _ctx.$vuetify.display.xs,
      scrollable: ""
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { class: "channel-dedup__card" }, {
          default: _withCtx(() => [
            _createVNode(_component_VCardTitle, { class: "channel-dedup__title" }, {
              default: _withCtx(() => [
                _createVNode(_component_VIcon, {
                  icon: "mdi-send",
                  color: "primary"
                }),
                _cache[116] || (_cache[116] = _createElementVNode("span", null, "共享下载审批", -1)),
                _createVNode(_component_VSpacer),
                (approvalLoading.value)
                  ? (_openBlock(), _createBlock(_component_VTooltip, {
                      key: 0,
                      text: "加载中"
                    }, {
                      activator: _withCtx(({ props: tip }) => [
                        _createVNode(_component_VProgressCircular, _mergeProps(tip, {
                          indeterminate: "",
                          color: "primary",
                          size: 20,
                          width: 2,
                          "aria-label": "加载中",
                          class: "channel-dedup__loading"
                        }), null, 16)
                      ]),
                      _: 1
                    }))
                  : _createCommentVNode("", true),
                _createVNode(_component_VTooltip, { text: "刷新资源记录" }, {
                  activator: _withCtx(({ props: tip }) => [
                    _createVNode(_component_VBtn, _mergeProps(tip, {
                      icon: "mdi-refresh",
                      variant: "text",
                      "aria-label": "刷新资源记录",
                      onClick: refreshApprovalRecords
                    }), null, 16)
                  ]),
                  _: 1
                }),
                _createVNode(_component_VTooltip, { text: "关闭" }, {
                  activator: _withCtx(({ props: tip }) => [
                    _createVNode(_component_VBtn, _mergeProps(tip, {
                      icon: "mdi-close",
                      variant: "text",
                      "aria-label": "关闭共享下载审批",
                      onClick: _cache[31] || (_cache[31] = $event => (channelDedupDialog.value = false))
                    }), null, 16)
                  ]),
                  _: 1
                })
              ]),
              _: 1
            }),
            _createVNode(_component_VCardText, { class: "channel-dedup__body" }, {
              default: _withCtx(() => [
                (channelDedupError.value)
                  ? (_openBlock(), _createBlock(_component_VAlert, {
                      key: 0,
                      type: "error",
                      variant: "tonal",
                      density: "compact"
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(channelDedupError.value), 1)
                      ]),
                      _: 1
                    }))
                  : _createCommentVNode("", true),
                (channelDedup.value.last_error)
                  ? (_openBlock(), _createBlock(_component_VAlert, {
                      key: 1,
                      type: "warning",
                      variant: "tonal",
                      density: "compact"
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(channelDedup.value.last_error), 1)
                      ]),
                      _: 1
                    }))
                  : _createCommentVNode("", true),
                _createVNode(_component_VTabs, {
                  modelValue: approvalTab.value,
                  "onUpdate:modelValue": _cache[32] || (_cache[32] = $event => ((approvalTab).value = $event)),
                  density: "compact"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_VTab, { value: "requests" }, {
                      default: _withCtx(() => [...(_cache[117] || (_cache[117] = [
                        _createTextVNode("本机下载申请", -1)
                      ]))]),
                      _: 1
                    }),
                    (sharedRecordsHost.value)
                      ? (_openBlock(), _createBlock(_component_VTab, {
                          key: 0,
                          value: "records"
                        }, {
                          default: _withCtx(() => [...(_cache[118] || (_cache[118] = [
                            _createTextVNode("共享下载记录", -1)
                          ]))]),
                          _: 1
                        }))
                      : _createCommentVNode("", true),
                    _createVNode(_component_VTab, { value: "pending" }, {
                      default: _withCtx(() => [...(_cache[119] || (_cache[119] = [
                        _createTextVNode("下载待核验", -1)
                      ]))]),
                      _: 1
                    })
                  ]),
                  _: 1
                }, 8, ["modelValue"]),
                (channelDedupDialog.value)
                  ? (_openBlock(), _createBlock(SharedSettingsPanel, {
                      key: 2,
                      ref_key: "sharedRecordsPanel",
                      ref: sharedRecordsPanel,
                      api: __props.api,
                      "plugin-base": pluginBase.value,
                      "record-tab": approvalTab.value,
                      onHostChange: _cache[33] || (_cache[33] = $event => (sharedRecordsHost.value = $event)),
                      onLoadingChange: _cache[34] || (_cache[34] = $event => (sharedRecordsLoading.value = $event))
                    }, null, 8, ["api", "plugin-base", "record-tab"]))
                  : _createCommentVNode("", true),
                (approvalTab.value === 'pending')
                  ? (_openBlock(), _createElementBlock("div", _hoisted_64, [
                      _createElementVNode("div", _hoisted_65, [
                        _createVNode(_component_VTextField, {
                          modelValue: activeResourceFilter.value.query,
                          "onUpdate:modelValue": _cache[35] || (_cache[35] = $event => ((activeResourceFilter.value.query) = $event)),
                          label: "搜索记录",
                          "prepend-inner-icon": "mdi-magnify",
                          clearable: "",
                          maxlength: "200",
                          density: "comfortable",
                          "hide-details": ""
                        }, null, 8, ["modelValue"]),
                        _createVNode(_component_VSelect, {
                          modelValue: activeResourceFilter.value.media_type,
                          "onUpdate:modelValue": _cache[36] || (_cache[36] = $event => ((activeResourceFilter.value.media_type) = $event)),
                          label: "类型筛选",
                          items: resourceTypes,
                          density: "comfortable",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      (!ledgerLoading.value && !resourceRows.value.items?.length)
                        ? (_openBlock(), _createElementBlock("div", _hoisted_66, _toDisplayString(activeResourceFilter.value.query || activeResourceFilter.value.media_type ? '无匹配记录' : '暂无记录'), 1))
                        : _createCommentVNode("", true),
                      _createElementVNode("div", _hoisted_67, [
                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(resourceRows.value.items, (row) => {
                          return (_openBlock(), _createElementBlock("article", {
                            key: row.key,
                            class: "channel-dedup__record"
                          }, [
                            _createElementVNode("div", _hoisted_68, [
                              _createElementVNode("div", _hoisted_69, [
                                _createElementVNode("div", _hoisted_70, [
                                  _createTextVNode(_toDisplayString(resourceRecordIdentity(row)) + " ", 1),
                                  (channelRecordSpecs(row))
                                    ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                                        _createTextVNode(" · " + _toDisplayString(channelRecordSpecs(row)), 1)
                                      ], 64))
                                    : _createCommentVNode("", true)
                                ])
                              ]),
                              _createElementVNode("div", _hoisted_71, [
                                (row.actionable)
                                  ? (_openBlock(), _createBlock(_component_VBtn, {
                                      key: 0,
                                      size: "small",
                                      density: "compact",
                                      variant: "tonal",
                                      class: "channel-dedup__pending-action",
                                      "prepend-icon": "mdi-check-circle-outline",
                                      disabled: row.manual_state === 'queued' || row.manual_state === 'processing',
                                      onClick: $event => (openPendingApproval(row))
                                    }, {
                                      default: _withCtx(() => [...(_cache[120] || (_cache[120] = [
                                        _createTextVNode(" 核验并继续下载 ", -1)
                                      ]))]),
                                      _: 1
                                    }, 8, ["disabled", "onClick"]))
                                  : _createCommentVNode("", true)
                              ])
                            ]),
                            _createElementVNode("strong", _hoisted_72, _toDisplayString(row.title || row.path || row.file_key || row.title_key), 1),
                            _createElementVNode("div", _hoisted_73, [
                              _createElementVNode("span", null, "最近更新：" + _toDisplayString(formatChannelTimestamp(row.last_seen)), 1),
                              _createElementVNode("span", null, "任务：" + _toDisplayString(row.task_name || '-'), 1),
                              (row.path)
                                ? (_openBlock(), _createElementBlock("span", _hoisted_74, "文件：" + _toDisplayString(row.path), 1))
                                : _createCommentVNode("", true)
                            ]),
                            _createElementVNode("p", _hoisted_75, _toDisplayString(row.reason || '身份或规格待核验'), 1),
                            (row.manual_state === 'queued' || row.manual_state === 'processing')
                              ? (_openBlock(), _createElementBlock("p", _hoisted_76, [
                                  _createVNode(_component_VProgressCircular, {
                                    indeterminate: "",
                                    size: "14",
                                    width: "2",
                                    class: "mr-2"
                                  }),
                                  _cache[121] || (_cache[121] = _createTextVNode(" 正在重新核验并执行下载审批 ", -1))
                                ]))
                              : (row.manual_error)
                                ? (_openBlock(), _createElementBlock("p", _hoisted_77, " 上次处理未完成：" + _toDisplayString(row.manual_error), 1))
                                : _createCommentVNode("", true),
                            (row.source_consumed)
                              ? (_openBlock(), _createElementBlock("p", _hoisted_78, " 同一种子中的其他待核验文件不可再次单独添加 "))
                              : _createCommentVNode("", true)
                          ]))
                        }), 128))
                      ]),
                      _createVNode(SharedRecordPagination, {
                        modelValue: resourcePage.value,
                        "onUpdate:modelValue": [
                          _cache[37] || (_cache[37] = $event => ((resourcePage).value = $event)),
                          loadResourceRows
                        ],
                        total: resourceRows.value.total
                      }, null, 8, ["modelValue", "total"])
                    ]))
                  : _createCommentVNode("", true)
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue", "width", "max-width", "height", "fullscreen"]),
    _createVNode(_component_VDialog, {
      modelValue: pendingApprovalDialog.value,
      "onUpdate:modelValue": _cache[47] || (_cache[47] = $event => ((pendingApprovalDialog).value = $event)),
      "max-width": "48rem"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { title: "人工核验并继续下载" }, {
          default: _withCtx(() => [
            _createVNode(_component_VCardText, { class: "pending-approval__body" }, {
              default: _withCtx(() => [
                _createVNode(_component_VAlert, {
                  type: "warning",
                  variant: "tonal",
                  density: "compact"
                }, {
                  default: _withCtx(() => [...(_cache[122] || (_cache[122] = [
                    _createTextVNode(" 你确认的身份和规格将重新进入原选种规则与共享下载审批；重复命中、规则排除或协调端拒绝时不会下载。 ", -1)
                  ]))]),
                  _: 1
                }),
                _createElementVNode("div", _hoisted_79, [
                  _createVNode(_component_VSelect, {
                    modelValue: pendingApprovalForm.value.media_type,
                    "onUpdate:modelValue": _cache[39] || (_cache[39] = $event => ((pendingApprovalForm.value.media_type) = $event)),
                    label: "媒体类型",
                    items: [{ title: '剧集', value: 'tv' }, { title: '电影', value: 'movie' }],
                    "hide-details": ""
                  }, null, 8, ["modelValue"]),
                  _createVNode(_component_VTextField, {
                    modelValue: pendingApprovalForm.value.tmdb_id,
                    "onUpdate:modelValue": _cache[40] || (_cache[40] = $event => ((pendingApprovalForm.value.tmdb_id) = $event)),
                    label: "TMDB ID",
                    inputmode: "numeric",
                    "hide-details": ""
                  }, {
                    "append-inner": _withCtx(() => [
                      _createVNode(_component_VBtn, {
                        icon: "mdi-magnify",
                        variant: "text",
                        size: "small",
                        type: "button",
                        "aria-label": "按名称查询媒体编号",
                        title: "按名称查询媒体编号",
                        "aria-expanded": pendingSearchOpen.value,
                        disabled: pendingApprovalSaving.value,
                        onClick: togglePendingSearch
                      }, null, 8, ["aria-expanded", "disabled"])
                    ]),
                    _: 1
                  }, 8, ["modelValue"]),
                  (pendingSearchOpen.value)
                    ? (_openBlock(), _createElementBlock("div", _hoisted_80, [
                        _createElementVNode("form", {
                          class: "pending-approval__query",
                          onSubmit: _withModifiers(searchPendingMedia, ["prevent"])
                        }, [
                          _createVNode(_component_VTextField, {
                            modelValue: pendingSearchQuery.value,
                            "onUpdate:modelValue": _cache[41] || (_cache[41] = $event => ((pendingSearchQuery).value = $event)),
                            label: "搜索媒体名称",
                            density: "comfortable",
                            clearable: "",
                            "hide-details": ""
                          }, null, 8, ["modelValue"]),
                          _createVNode(_component_VBtn, {
                            type: "submit",
                            variant: "tonal",
                            color: "primary",
                            "prepend-icon": "mdi-magnify",
                            loading: pendingSearchLoading.value,
                            disabled: !pendingSearchQuery.value?.trim()
                          }, {
                            default: _withCtx(() => [...(_cache[123] || (_cache[123] = [
                              _createTextVNode("搜索", -1)
                            ]))]),
                            _: 1
                          }, 8, ["loading", "disabled"])
                        ], 32),
                        (pendingSearchMessage.value)
                          ? (_openBlock(), _createElementBlock("div", _hoisted_81, _toDisplayString(pendingSearchMessage.value), 1))
                          : _createCommentVNode("", true),
                        (pendingSearchResults.value.length)
                          ? (_openBlock(), _createBlock(_component_VList, {
                              key: 1,
                              class: "pending-approval__results",
                              density: "compact"
                            }, {
                              default: _withCtx(() => [
                                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(pendingSearchResults.value, (row) => {
                                  return (_openBlock(), _createBlock(_component_VListItem, {
                                    key: `${row.type}:${row.id}`,
                                    title: row.title,
                                    subtitle: [row.type === 'tv' ? '剧集' : '电影', row.year, `TMDB ${row.id}`].filter(Boolean).join(' · '),
                                    onClick: $event => (selectPendingMedia(row))
                                  }, null, 8, ["title", "subtitle", "onClick"]))
                                }), 128))
                              ]),
                              _: 1
                            }))
                          : _createCommentVNode("", true)
                      ]))
                    : _createCommentVNode("", true),
                  _createVNode(_component_VTextField, {
                    modelValue: pendingApprovalForm.value.media_title,
                    "onUpdate:modelValue": _cache[42] || (_cache[42] = $event => ((pendingApprovalForm.value.media_title) = $event)),
                    label: "中文媒体名",
                    "hide-details": "",
                    class: "pending-approval__name"
                  }, null, 8, ["modelValue"]),
                  (pendingApprovalForm.value.media_type === 'tv')
                    ? (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                        _createVNode(_component_VTextField, {
                          modelValue: pendingApprovalForm.value.season,
                          "onUpdate:modelValue": _cache[43] || (_cache[43] = $event => ((pendingApprovalForm.value.season) = $event)),
                          label: "季号",
                          type: "number",
                          min: "0",
                          max: "999",
                          "hide-details": "",
                          placeholder: pendingApprovalRow.value?.source_season ? 'TMDB 标准季号' : undefined,
                          loading: pendingSearchLoading.value && !pendingSearchOpen.value
                        }, null, 8, ["modelValue", "placeholder", "loading"]),
                        _createVNode(_component_VTextField, {
                          modelValue: pendingApprovalForm.value.episodes,
                          "onUpdate:modelValue": _cache[44] || (_cache[44] = $event => ((pendingApprovalForm.value.episodes) = $event)),
                          label: "集号",
                          placeholder: "1, 2-4",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ], 64))
                    : _createCommentVNode("", true),
                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(channelDedupAttributes.filter(item =>
              channelDedup.value.attributes.includes(item.value)), (field) => {
                    return (_openBlock(), _createBlock(_component_VTextField, {
                      key: field.value,
                      modelValue: pendingApprovalForm.value.specs[field.value],
                      "onUpdate:modelValue": $event => ((pendingApprovalForm.value.specs[field.value]) = $event),
                      label: field.value === 'size' ? '大小（字节）' : field.title,
                      type: ['size', 'fps'].includes(field.value) ? 'number' : 'text',
                      min: ['size', 'fps'].includes(field.value) ? 0.001 : undefined,
                      "hide-details": ""
                    }, null, 8, ["modelValue", "onUpdate:modelValue", "label", "type", "min"]))
                  }), 128))
                ]),
                _createVNode(_component_VCheckbox, {
                  modelValue: pendingApprovalForm.value.confirmed,
                  "onUpdate:modelValue": _cache[45] || (_cache[45] = $event => ((pendingApprovalForm.value.confirmed) = $event)),
                  density: "compact",
                  "hide-details": "",
                  label: "我已核对以上媒体身份、季集号及规格"
                }, null, 8, ["modelValue"]),
                (pendingApprovalError.value)
                  ? (_openBlock(), _createBlock(_component_VAlert, {
                      key: 0,
                      type: "error",
                      variant: "tonal",
                      density: "compact"
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(pendingApprovalError.value), 1)
                      ]),
                      _: 1
                    }))
                  : _createCommentVNode("", true),
                (pendingApprovalRow.value)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_82, " 文件：" + _toDisplayString(pendingApprovalRow.value.path), 1))
                  : _createCommentVNode("", true)
              ]),
              _: 1
            }),
            _createVNode(_component_VCardActions, null, {
              default: _withCtx(() => [
                _createVNode(_component_VSpacer),
                _createVNode(_component_VBtn, {
                  variant: "text",
                  disabled: pendingApprovalSaving.value,
                  onClick: _cache[46] || (_cache[46] = $event => (pendingApprovalDialog.value = false))
                }, {
                  default: _withCtx(() => [...(_cache[124] || (_cache[124] = [
                    _createTextVNode("取消", -1)
                  ]))]),
                  _: 1
                }, 8, ["disabled"]),
                _createVNode(_component_VBtn, {
                  color: "primary",
                  variant: "flat",
                  loading: pendingApprovalSaving.value,
                  disabled: !pendingApprovalForm.value.confirmed || (pendingSearchLoading.value && !pendingSearchOpen.value),
                  onClick: submitPendingApproval
                }, {
                  default: _withCtx(() => [...(_cache[125] || (_cache[125] = [
                    _createTextVNode(" 重新核验并提交 ", -1)
                  ]))]),
                  _: 1
                }, 8, ["loading", "disabled"])
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"]),
    _createVNode(_component_VDialog, {
      modelValue: deleteDialog.value,
      "onUpdate:modelValue": _cache[49] || (_cache[49] = $event => ((deleteDialog).value = $event)),
      "max-width": "28rem"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { title: "删除刷流任务" }, {
          default: _withCtx(() => [
            _createVNode(_component_VCardText, null, {
              default: _withCtx(() => [
                _createTextVNode("确认删除“" + _toDisplayString(selectedTask.value?.name) + "”？存在活跃种子时不会执行删除。", 1)
              ]),
              _: 1
            }),
            _createVNode(_component_VCardActions, null, {
              default: _withCtx(() => [
                _createVNode(_component_VSpacer),
                _createVNode(_component_VBtn, {
                  variant: "text",
                  onClick: _cache[48] || (_cache[48] = $event => (deleteDialog.value = false))
                }, {
                  default: _withCtx(() => [...(_cache[126] || (_cache[126] = [
                    _createTextVNode("取消", -1)
                  ]))]),
                  _: 1
                }),
                _createVNode(_component_VBtn, {
                  color: "error",
                  variant: "flat",
                  loading: saving.value,
                  onClick: confirmDeleteTask
                }, {
                  default: _withCtx(() => [...(_cache[127] || (_cache[127] = [
                    _createTextVNode("删除", -1)
                  ]))]),
                  _: 1
                }, 8, ["loading"])
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"]),
    _createVNode(_component_VDialog, {
      modelValue: clearDialog.value,
      "onUpdate:modelValue": _cache[51] || (_cache[51] = $event => ((clearDialog).value = $event)),
      "max-width": "28rem"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { title: "清除任务数据" }, {
          default: _withCtx(() => [
            _createVNode(_component_VCardText, null, {
              default: _withCtx(() => [...(_cache[128] || (_cache[128] = [
                _createTextVNode("将清除当前任务的统计、运行诊断、托管和归档记录，下载器内的种子与文件不会删除。", -1)
              ]))]),
              _: 1
            }),
            _createVNode(_component_VCardActions, null, {
              default: _withCtx(() => [
                _createVNode(_component_VSpacer),
                _createVNode(_component_VBtn, {
                  variant: "text",
                  onClick: _cache[50] || (_cache[50] = $event => (clearDialog.value = false))
                }, {
                  default: _withCtx(() => [...(_cache[129] || (_cache[129] = [
                    _createTextVNode("取消", -1)
                  ]))]),
                  _: 1
                }),
                _createVNode(_component_VBtn, {
                  color: "warning",
                  variant: "flat",
                  loading: saving.value,
                  onClick: confirmClearTask
                }, {
                  default: _withCtx(() => [...(_cache[130] || (_cache[130] = [
                    _createTextVNode("清除", -1)
                  ]))]),
                  _: 1
                }, 8, ["loading"])
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"])
  ], 2))
}
}

};
const ResourceFlowWorkbench = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-11bc9690"]]);

export { ResourceFlowWorkbench as R };
