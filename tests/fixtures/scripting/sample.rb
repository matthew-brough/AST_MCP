# Top level module.
module Billing
  # An invoice.
  class Invoice
    # Returns the total.
    def total(lines)
      lines.sum
    end

    def self.build(id)
      new
    end
  end
end
