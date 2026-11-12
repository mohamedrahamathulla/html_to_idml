// html2idml - fit pages to text
// Run in InDesign (Window > Utilities > Scripts, or double-click from the Scripts panel)
// with the converted document open.  It adds pages while the main story is overset and
// removes empty trailing pages, so the page count matches the content exactly.
#target "InDesign"
(function () {
    if (app.documents.length === 0) { alert("Open the converted document first."); return; }
    var doc = app.activeDocument;
    var oldOrigin = doc.viewPreferences.rulerOrigin;
    doc.viewPreferences.rulerOrigin = RulerOrigin.PAGE_ORIGIN;
    var added = 0, removed = 0;
    try {
        var stories = doc.stories;
        for (var s = 0; s < stories.length; s++) {
            var story = stories[s];
            var guard = 0;
            while (story.overflows && guard < 3000) {
                guard++;
                var frames = story.textContainers;
                var last = frames[frames.length - 1];
                if (!(last instanceof TextFrame) || !last.parentPage) { break; }
                var page = doc.pages.add(LocationOptions.AT_END);
                var mp = page.marginPreferences;
                var b = page.bounds;
                var left = (page.side == PageSideOptions.LEFT_HAND) ? mp.right : mp.left;
                var right = (page.side == PageSideOptions.LEFT_HAND) ? mp.left : mp.right;
                var tf = page.textFrames.add({ geometricBounds: [b[0] + mp.top, b[1] + left, b[2] - mp.bottom, b[3] - right] });
                tf.textFramePreferences.textColumnCount = last.textFramePreferences.textColumnCount;
                tf.textFramePreferences.textColumnGutter = last.textFramePreferences.textColumnGutter;
                last.nextTextFrame = tf;
                story.recompose();
                added++;
                if (tf.characters.length === 0 && story.overflows) {
                    // nothing could flow in (an element is larger than the frame) - stop
                    page.remove(); added--; break;
                }
            }
        }
        while (doc.pages.length > 1) {
            var p = doc.pages.lastItem();
            var items = p.allPageItems, empty = true;
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                if (it instanceof TextFrame) { if (it.characters.length > 0) { empty = false; break; } }
                else if (it.parent === p || it.parentPage === p) { empty = false; break; }
            }
            if (!empty) { break; }
            p.remove(); removed++;
        }
    } finally {
        doc.viewPreferences.rulerOrigin = oldOrigin;
    }
    alert("html2idml: " + added + " page(s) added, " + removed + " empty page(s) removed.\nPages: " + doc.pages.length);
})();
