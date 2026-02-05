(set-logic QF_BV)
;; Simple 8-bit arithmetic to avoid yinyang's bitwise typecheck bug
(declare-const x (_ BitVec 8))
(declare-const y (_ BitVec 8))
(assert (= (bvadd x y) (_ bv42 8)))
(assert (bvult x y))
(check-sat)
(get-model)
