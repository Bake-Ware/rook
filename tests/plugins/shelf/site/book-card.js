/* The shelf plugin's component (sample): one book. Any page may use it as <book-card>. */
export default {
  props: { book: Object },
  template: `<section class="panel book-card"><div class="panel-head"><h2>{{ book.title }}</h2><span class="sub">{{ book.author }}</span></div></section>`,
};
