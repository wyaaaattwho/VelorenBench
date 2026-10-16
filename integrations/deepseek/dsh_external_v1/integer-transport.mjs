export function assertExactIntegerArguments(schema, value, path = 'arguments') {
  if (schema.type === 'integer' && typeof value === 'string' && !/^(?:0|-?[1-9][0-9]*)$/.test(value)) {
    throw new Error(`${path}: integer token must be canonical decimal; no native submission`)
  }
  if (schema.type === 'integer' && typeof value === 'number' && !Number.isSafeInteger(value)) {
    throw new Error(`${path}: integer is not exactly representable in JavaScript; no native submission, never round or clamp`)
  }
  for (const keyword of ['oneOf', 'anyOf']) {
    for (const branch of schema[keyword] ?? []) {
      if (branch.type === 'null' && value !== null) continue
      if (branch.properties && value && typeof value === 'object') {
        const mismatch = Object.entries(branch.properties).some(([key, field]) =>
          field.enum && key in value && !field.enum.includes(value[key]))
        if (mismatch) continue
      }
      assertExactIntegerArguments(branch, value, path)
    }
  }
  if (schema.properties && value && typeof value === 'object' && !Array.isArray(value)) {
    for (const [key, field] of Object.entries(schema.properties)) {
      if (key in value) assertExactIntegerArguments(field, value[key], `${path}.${key}`)
    }
  }
  if (schema.items && Array.isArray(value)) {
    value.forEach((item, index) => assertExactIntegerArguments(schema.items, item, `${path}[${index}]`))
  }
}
