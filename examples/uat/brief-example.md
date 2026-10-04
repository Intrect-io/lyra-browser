# Round brief (example)

This round looks at the path from "interested" to "ready to pay": unclear
prices, a button that does nothing or goes somewhere unexpected, a checkout
that shows the wrong product or currency, copy that contradicts itself.

Where you reach a live checkout you may open it once and read it — product,
price, currency, what it asks for first — then leave. Never type into it.

Add a `purchase_path` to your `finish` call: every step from the first price
you saw to the checkout or to where you stopped, with what each step promised
and what it did.
