<script setup>
defineProps({
  modelValue: { type: Number, required: true },
  total: { type: Number, required: true },
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <div class="shared-settings__pagination">
    <div v-if="total > 20" class="shared-settings__pagination-control">
      <VPagination :model-value="modelValue" :length="Math.ceil(total / 20)" :total-visible="3"
        @update:model-value="emit('update:modelValue', $event)" />
    </div>
    <span class="shared-settings__total text-caption text-medium-emphasis">共 {{ total }} 条</span>
  </div>
</template>

<style scoped>
.shared-settings__pagination { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; min-block-size: 52px; margin-block-start: 8px; }
.shared-settings__pagination-control { grid-column: 2; min-inline-size: 0; }
.shared-settings__total { grid-column: 3; justify-self: end; white-space: nowrap; }
@media (max-width: 599px) {
  .shared-settings__pagination { grid-template-columns: minmax(0, 1fr) auto; column-gap: 8px; }
  .shared-settings__pagination-control { grid-column: 1; }
  .shared-settings__total { grid-column: 2; }
}
</style>
