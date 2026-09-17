# Payment.Validate does not validate InvoiceID (and CheckCreate.Validate has the same gap)

## Summary

`Payment.Validate()` in `xrpl/transaction/payment.go` checks `Amount`, `Destination`, `Paths`, `SendMax`, `DeliverMax`, `DeliverMin`, `CredentialIDs`, the partial-payment flag, and `DomainID`, but never validates `InvoiceID`. A `Payment` with `InvoiceID: "not-a-hash"` passes `Validate()` and is only rejected later by the binary codec or by rippled.

`CheckCreate.Validate()` in `xrpl/transaction/check_create.go` has the same gap: `InvoiceID` is flattened (line 60) but not validated.

`DestinationTag` is also unchecked, but as a `*uint32` every value is valid, so that one needs no change.

## Where

- `xrpl/transaction/payment.go`, `Validate()` (lines 181-237 at `af7ba71`): field declared at line 61, flattened at lines 123-124, no check in `Validate`.
- `xrpl/transaction/check_create.go`: field at line 39, flattened at line 60, no check in `Validate`.

## Expected

The project already validates 256-bit hash fields elsewhere with the same idiom, for example `CheckCancel.Validate` and `CheckCash.Validate`:

```go
if !typecheck.IsHex(c.CheckID.String()) || len(c.CheckID.String()) != 64 {
    return false, ErrInvalidCheckID
}
```

`InvoiceID` is a `types.Hash256` and should get the equivalent optional check:

```go
if p.InvoiceID != "" && (!typecheck.IsHex(p.InvoiceID.String()) || len(p.InvoiceID.String()) != 64) {
    return false, ErrInvalidInvoiceID
}
```

with a new sentinel `ErrInvalidInvoiceID` in `xrpl/transaction/errors.go`, plus `fail - invalid InvoiceID` cases in `TestPayment_Validate` and `TestCheckCreate_Validate`.

A shared helper (`IsHash256(string) bool` in `validations_xrpl_objects.go`) would let `CheckID`, `InvoiceID`, `WalletLocator` and friends use one implementation instead of repeating the hex-and-length check.

## How this was found

While researching a convention-scoring review tool, a per-field "does `Validate` check field X?" question over the function slice answered yes at 0.98 or higher for every field except `DestinationTag` (0.05) and `InvoiceID` (0.04). Verified by reading the source.
