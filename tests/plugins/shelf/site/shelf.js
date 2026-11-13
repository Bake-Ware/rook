/* The shelf plugin's page (sample): it calls its cap shelf.books with the site's call()
   helper, as the signed-in person, and shows each book with its own component, book-card. */
import { call, viewHref } from '/site/shell.js';

export default {
  data() { return { books: null, error: '' }; },
  computed: { href() { return viewHref(location.pathname, {}); } },
  async mounted() {
    try { this.books = (await call('hub', 'shelf.books', {}, 'Listed on the Shelf page.')).books; }
    catch (e) { this.error = e.message; }
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Shelf</h1><pin-view label="Library: Shelf" :href="href"></pin-view></div>
        <p>A sample plugin page: the books come from the cap shelf.books.</p></div>
    </header>
    <div class="wrap">
      <p v-if="error" class="callout" role="alert">{{ error }}</p>
      <p v-if="books" class="small dim2">shelf.books answered {{ books.length }} books.</p>
      <book-card v-for="b in books || []" :key="b.title" :book="b"></book-card>
    </div>
  </div>`,
};
