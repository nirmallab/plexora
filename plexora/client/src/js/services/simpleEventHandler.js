
class SimpleEventHandler {

    constructor(subject) {
        this.subject = subject;
        this.eventListeners = [];
    }

    /**
     * @returns a function that removes what this call bound. The wrapper is
     * the listener the subject holds, so only this call can name it -- without
     * the return value a handler bound here could never be taken off again.
     */
    bind(eventNames, eventFunction) {
        const bound = [];
        for (const eventName of eventNames.split(' ')) {
            const entry = {eventName, eventFunction};
            this.eventListeners.push(entry);
            const eventFunctionWrap = e => eventFunction(e.detail, e);
            this.subject.addEventListener(eventName, eventFunctionWrap, false);
            bound.push({entry, eventFunctionWrap});
        }
        return () => {
            for (const {entry, eventFunctionWrap} of bound.splice(0)) {
                this.subject.removeEventListener(entry.eventName, eventFunctionWrap, false);
                const at = this.eventListeners.indexOf(entry);
                if (at >= 0) this.eventListeners.splice(at, 1);
            }
        };
    }

    getListeners() {
        return this.eventListeners;
    }

    trigger(eventName, detail) {
        this.subject.dispatchEvent(new CustomEvent(eventName, {detail}));
    }

}
