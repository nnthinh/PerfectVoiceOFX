"use strict";

const time = require("./time");
const dedupe = require("./dedupe");
const reject = require("./reject");
const selection = require("./selection");
const inspect = require("./inspect");
const place = require("./place");

module.exports = {
    ...time,
    ...dedupe,
    ...reject,
    ...selection,
    ...inspect,
    ...place,
};
