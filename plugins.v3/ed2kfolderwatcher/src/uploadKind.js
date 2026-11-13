const badges = {
  instant: {
    kind: 'instant',
    label: '秒传',
    title: '上传结果为秒传，不触发FUCK孤本转发；其他输出和转发规则不变。',
  },
  ordinary: {
    kind: 'ordinary',
    label: '普通上传',
    title: '上传结果为普通上传；启用FUCK孤本后，内容核验通过才投递，投递结果另行显示。',
  },
  unknown: {
    kind: 'unknown',
    label: '上传方式未知',
    title: '尚无可靠的秒传或普通上传结果，可能仍在处理中、接口未提供或历史记录未保存；不据此触发FUCK孤本转发。',
  },
}

export function uploadKindBadge(item) {
  if (!item.upload_mode && !item.upload_state && !item.upload_kind) return null
  if (item.upload_kind === 'instant') return badges.instant
  if (item.upload_kind === 'ordinary') return badges.ordinary
  return badges.unknown
}
