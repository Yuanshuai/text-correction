<script setup>
// 一段文字：有改动就按标记切成一个个 span（只上色），没改动直接当纯文本渲染。
// 段落和表格单元格都用它，免得同一套标记逻辑写两遍。
const props = defineProps({
  tokens: { type: Array, default: null },
  text: { type: String, default: '' },
  active: { type: Number, default: -1 },
  // 已忽略的那些改动（Set）：正文里也跟着灰掉，不然页面上还是一片花
  ignored: { type: Object, default: null },
})

const emit = defineEmits(['pick'])

function tokenClass(token) {
  return [
    token.cls,
    {
      'is-active': token.idx >= 0 && token.idx === props.active,
      'is-ignored': token.idx >= 0 && Boolean(props.ignored?.has(token.idx)),
    },
  ]
}
</script>

<template>
  <span v-if="tokens" class="mk">
    <span
      v-for="(token, i) in tokens"
      :key="i"
      :class="tokenClass(token)"
      :data-mark="token.idx >= 0 ? token.idx : null"
      @click="token.idx >= 0 && emit('pick', token.idx)"
    >{{ token.char }}</span>
  </span>
  <template v-else>{{ text }}</template>
</template>
