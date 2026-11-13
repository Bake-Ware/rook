/* Small parts shared by the Jobs, Secrets and Auth pages. */

/* How long ago an ISO time was (or how long until it, if it is ahead), in a few words. */
export function ago(iso) {
  if (!iso) return 'never';
  const span = s => s < 60 ? s + ' s' : s < 3600 ? Math.round(s / 60) + ' min' : s < 86400 ? Math.round(s / 3600) + ' h' : Math.round(s / 86400) + ' d';
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  return s >= 0 ? span(s) + ' ago' : 'in ' + span(-s);
}

/* A delete or revoke button that asks a plain "are you sure" in place before it acts. */
export const SureButton = {
  props: { label: String, question: String },
  emits: ['yes'],
  data() { return { asking: false }; },
  template: `<span style="display:inline-flex;gap:6px;align-items:center" @click.stop>
    <button v-if="!asking" class="btn sm danger" type="button" @click="asking=true">{{ label }}</button>
    <template v-else><span class="note">{{ question }}</span>
      <button class="btn sm danger" type="button" @click="asking=false; $emit('yes')">Yes</button>
      <button class="btn sm ghost" type="button" @click="asking=false">Cancel</button></template>
  </span>`,
};
